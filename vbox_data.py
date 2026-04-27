"""
vbox_data.py — Racelogic VBOX .vbo loader
==========================================
Parses the text-based VBOX format.  Files are divided into named sections
([header], [channel units], [channel names], [comments], [data]) and data
rows are whitespace-delimited.

Coordinate format : DDMM.MMMMM (degrees + decimal minutes) → decimal degrees
Time format       : HHMMSS.SS combined with date from [comments]
Speed             : 'velocity kmh' in km/h; bare 'velocity' assumed knots
G-forces          : G units — lateral-acc → gforce_y, longitudinal-acc → gforce_x
Lap detection     : 'lap trigger' channel counter if present, else single lap (1)
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from math import floor
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from data_model import DataPoint, Session, build_laps_from_points

logger = logging.getLogger(__name__)

_INLAP_SLOWNESS_THRESHOLD = 1.5


# ── Public detection ──────────────────────────────────────────────────────────

def is_vbox(path: str) -> bool:
    """Return True if *path* is a Racelogic VBOX text file."""
    if Path(path).suffix.lower() != '.vbo':
        return False
    try:
        with open(path, 'r', encoding='utf-8-sig', errors='ignore') as f:
            head = f.read(512)
        return '[header]' in head.lower()
    except Exception:
        return False


# ── Parsing helpers ───────────────────────────────────────────────────────────

def _parse_sections(path: str) -> Dict[str, List[str]]:
    """Split a .vbo file into named sections; skip blank lines within sections."""
    sections: Dict[str, List[str]] = {}
    current: Optional[str] = None
    with open(path, 'r', encoding='utf-8-sig', errors='ignore') as f:
        for line in f:
            line = line.rstrip('\n\r')
            if line.startswith('[') and line.endswith(']'):
                current = line[1:-1].strip().lower()
                sections[current] = []
            elif current is not None and line.strip():
                sections[current].append(line)
    return sections


def _parse_vbox_created_time(path: str, comments: str) -> Optional[datetime]:
    """Extract absolute VBOX creation datetime from file preamble/comments."""
    try:
        with open(path, 'r', encoding='utf-8-sig', errors='ignore') as f:
            head = f.read(4096)
    except Exception:
        head = ''
    text = '\n'.join([head, comments])
    # Supports both "File created on ..." and "File created in ..."
    m = re.search(
        r'file\s+created\s+(?:on|in)\s+(\d{2})/(\d{2})/(\d{4})(?:\s+at\s+(\d{2}):(\d{2}):(\d{2}))?',
        text,
        flags=re.IGNORECASE,
    )
    if not m:
        return None
    day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    hh = int(m.group(4) or 0)
    mm = int(m.group(5) or 0)
    ss = int(m.group(6) or 0)
    return datetime(year, month, day, hh, mm, ss, tzinfo=timezone.utc)


def _dms_to_decimal(raw: float, hemisphere: Optional[str]) -> float:
    """Convert Racelogic DDMM.MMMMM encoding to decimal degrees.

    When hemisphere is unknown from channel names, preserve the sign encoded
    in the numeric value itself (common in custom-exported VBO files).
    """
    deg = floor(abs(raw) / 100)
    minutes = abs(raw) - deg * 100
    decimal = deg + minutes / 60.0
    if hemisphere in ('S', 'W'):
        return -decimal
    if hemisphere in ('N', 'E'):
        return decimal
    return -decimal if raw < 0 else decimal


def _parse_hhmmss(raw: float) -> Tuple[int, int, float]:
    """Decompose HHMMSS.SS float into (hours, minutes, seconds)."""
    h = int(raw) // 10000
    m = (int(raw) // 100) % 100
    s = round(raw - h * 10000 - m * 100, 6)
    return h, m, s


# ── Main loader ───────────────────────────────────────────────────────────────

def load_vbo(path: str) -> Session:
    sections = _parse_sections(path)
    header_lines = [c.strip().lower() for c in sections.get('header', []) if c.strip()]

    # Prefer explicit data columns when available. Some VBO variants keep
    # telemetry channels in [header] but store true row schema in [column names].
    # If we only parse [header], channels like "lap" may be missed.
    column_name_lines = sections.get('column names', [])
    if column_name_lines:
        channels = [c.strip().lower() for c in column_name_lines[0].split() if c.strip()]
    else:
        if not header_lines:
            raise ValueError(f"No [header] section in {path}")
        channels = header_lines

    unit_lines = [u.strip().lower() for u in sections.get('channel units', [])]
    units: Dict[str, str] = dict(zip(channels, unit_lines)) if unit_lines else {}

    # ── Channel index lookup ──────────────────────────────────────────────────

    def _find(*names: str) -> Optional[int]:
        for name in names:
            for i, ch in enumerate(channels):
                if ch == name or ch.startswith(name):
                    return i
        return None

    idx_time    = _find('time')
    idx_lat     = _find('latitude north', 'latitude south', 'latitude', 'lat')
    idx_lon     = _find('longitude east', 'longitude west', 'longitude', 'long', 'lon')
    idx_speed   = _find('velocity kmh', 'velocity mph', 'velocity', 'speed')
    idx_height  = _find('height', 'altitude')
    idx_lat_g   = _find('lateral-acc', 'lateral acc', 'ay', 'latacc', 'lat_acc')
    idx_lon_g   = _find('longitudinal-acc', 'longitudinal acc', 'ax', 'longacc', 'long_acc')
    idx_vert_g  = _find('az', 'vertical-acc', 'vertical acc')
    idx_lap     = _find('lap', 'lap trigger', 'lap-trigger', 'lapctr', 'lap beacon', 'lap count')
    idx_lap_elapsed = _find('lap_elapsed_s', 'lap_elapsed', 'lapelapsed')
    idx_rpm     = _find('rpm')
    idx_yaw     = _find('yaw rate', 'yaw-rate')

    if idx_time is None or idx_lat is None or idx_lon is None:
        raise ValueError(f"Missing required channels (time/lat/lon) in {path}")

    # Hemisphere: read from channel name
    lat_hem: Optional[str] = None
    lon_hem: Optional[str] = None
    for c in channels:
        if 'latitude' in c:
            if 'south' in c:
                lat_hem = 'S'
            elif 'north' in c:
                lat_hem = 'N'
        if 'longitude' in c:
            if 'west' in c:
                lon_hem = 'W'
            elif 'east' in c:
                lon_hem = 'E'

    # Speed conversion factor
    speed_ch = channels[idx_speed] if idx_speed is not None else ''
    speed_unit = units.get(speed_ch, '')
    speed_hint = header_lines[idx_speed] if idx_speed is not None and idx_speed < len(header_lines) else ''
    speed_unit_by_idx = unit_lines[idx_speed] if idx_speed is not None and idx_speed < len(unit_lines) else ''
    speed_ctx = ' '.join([speed_ch, speed_unit, speed_hint, speed_unit_by_idx]).lower()
    if 'kmh' in speed_ctx or 'km/h' in speed_ctx or 'kph' in speed_ctx:
        speed_factor = 1.0
    elif 'mph' in speed_ctx:
        speed_factor = 1.60934
    elif 'm/s' in speed_ctx:
        speed_factor = 3.6
    else:
        speed_factor = 1.852  # bare 'velocity' → knots

    # Session date from [comments]
    comments_text = '\n'.join(sections.get('comments', []))
    created_dt = _parse_vbox_created_time(path, comments_text)
    session_date = None
    if created_dt is not None:
        # Point timestamps should use the VBO date + row HHMMSS time.
        session_date = datetime(created_dt.year, created_dt.month, created_dt.day, tzinfo=timezone.utc)

    # ── Data rows ─────────────────────────────────────────────────────────────

    data_lines = sections.get('data', [])
    if not data_lines:
        raise ValueError(f"No [data] section in {path}")

    all_pts: List[DataPoint] = []
    prev_dt: Optional[datetime] = None
    day_offset = 0

    for record_idx, line in enumerate(data_lines):
        cols = line.split()
        min_idx = max(c for c in [idx_time, idx_lat, idx_lon] if c is not None)
        if len(cols) <= min_idx:
            continue

        def _col(idx: Optional[int], default: float = 0.0) -> float:
            if idx is None or idx >= len(cols):
                return default
            try:
                return float(cols[idx])
            except ValueError:
                return default

        h, m, s = _parse_hhmmss(_col(idx_time))
        if session_date is not None:
            dt = session_date + timedelta(hours=h, minutes=m, seconds=s, days=day_offset)
            if prev_dt is not None and (dt - prev_dt).total_seconds() < -3600:
                day_offset += 1
                dt += timedelta(days=1)
        else:
            dt = datetime(1970, 1, 1, h, m, int(s),
                          microsecond=int((s % 1) * 1_000_000),
                          tzinfo=timezone.utc)
        prev_dt = dt

        lat = _dms_to_decimal(_col(idx_lat), lat_hem)
        lon = _dms_to_decimal(_col(idx_lon), lon_hem)

        speed  = _col(idx_speed) * speed_factor
        lat_g  = _col(idx_lat_g)   # → gforce_y (lateral)
        lon_g  = _col(idx_lon_g)   # → gforce_x (longitudinal)
        vert_g = _col(idx_vert_g)
        height = _col(idx_height)
        rpm    = _col(idx_rpm)
        yaw    = _col(idx_yaw)     # deg/s; stored in gyro_z slot

        # lap trigger increments at each beacon crossing (0 = outlap)
        lap_num = int(_col(idx_lap)) if idx_lap is not None else 1
        lap_elapsed_hint = _col(idx_lap_elapsed, 0.0) if idx_lap_elapsed is not None else 0.0

        all_pts.append(DataPoint(
            record     = record_idx,
            time       = dt,
            lat        = lat,
            lon        = lon,
            alt        = height,
            speed      = speed,
            gforce_x   = lon_g,
            gforce_y   = lat_g,
            gforce_z   = vert_g,
            lap        = lap_num,
            gyro_x     = 0.0,
            gyro_y     = 0.0,
            gyro_z     = yaw,
            rpm        = rpm,
            lap_elapsed= max(0.0, lap_elapsed_hint),
        ))

    if not all_pts:
        raise ValueError(f"No valid data rows parsed from {path}")

    # ── Elapsed times ─────────────────────────────────────────────────────────

    t0 = all_pts[0].time
    for pt in all_pts:
        pt.elapsed = (pt.time - t0).total_seconds()

    # ── Build laps ────────────────────────────────────────────────────────────

    laps = build_laps_from_points(all_pts, outlap_lap_num=0)

    # Default policy for lap-tagged VBO: first lap is outlap, last lap is inlap.
    # Keep at least one timed lap for 2-lap files.
    # These tags can later be overridden manually in the UI.
    for l in laps:
        l.is_outlap = False
        l.is_inlap = False
    if len(laps) >= 3:
        laps[0].is_outlap = True
        laps[-1].is_inlap = True

    best_lap_time = min((l.duration for l in laps if not l.is_outlap and not l.is_inlap), default=0.0)
    # Prefer absolute creation datetime for session-level display metadata.
    date_str = created_dt.strftime('%Y-%m-%dT%H:%M:%SZ') if created_dt else (session_date.strftime('%Y-%m-%dT%H:%M:%SZ') if session_date else '')

    return Session(
        source        = 'VBOX',
        date_utc      = date_str,
        track         = '',
        configuration = '',
        session_type  = '',
        best_lap_time = best_lap_time,
        all_points    = all_pts,
        laps          = laps,
        is_bike       = False,
        csv_path      = path,
    )
