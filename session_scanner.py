"""
session_scanner.py — Session matcher and state manager
=======================================================
Scans folders for telemetry files (RaceBox CSV, AIM XRK, MoTeC LD, GPX)
and video files, matches them by timestamp proximity, and persists
processing state so runs can be resumed after interruption.

Matching strategy:
  1. Parse session start time from CSV metadata (Date UTC field, etc.).
  2. Parse session **end** time from CSV tail when possible (last sample timestamp
     or AIM ``Time (s)`` span) so the telemetry interval ``[start, end]`` is known.
  3. Extract video creation time from:
     a. ffprobe QuickTime creation_time metadata  (most accurate)
     b. File modification time                    (fallback)
  4. Group video segments that start within MAX_GAP seconds of each other
     into one "video group" — these belong to the same recording session.
  5. Match each CSV to a video group whose **wall-clock interval overlaps**
     ``[csv_start, csv_end]`` with ``[group.start_time, group.end_time]``; pick the
     largest overlap. If nothing overlaps, fall back to **closest group start**
     within ``MATCH_WINDOW`` seconds (legacy behaviour).
"""

from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)

from utils import _run
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable, List, Optional, Dict, Tuple  # noqa: F401 – Tuple used in scan_pending_xrk

VIDEO_EXTENSIONS = {'.mp4', '.mov', '.avi', '.mkv', '.MP4', '.MOV', '.AVI', '.MKV'}
CSV_EXTENSIONS   = {'.csv', '.CSV'}
GPX_EXTENSIONS   = {'.gpx', '.GPX'}
LD_EXTENSIONS    = {'.ld',  '.LD'}
VBO_EXTENSIONS   = {'.vbo', '.VBO'}
MAX_GAP          = 120.0    # seconds between consecutive segments of one recording
MATCH_WINDOW     = 3600.0   # max seconds between CSV start and video group start

# Sentinel stored on MatchedSession.csv_path entries to identify source type
CSV_SOURCE_RACEBOX = 'racebox'
CSV_SOURCE_AIM     = 'aim'
CSV_SOURCE_MOTEC   = 'motec'


# ── Video file info ────────────────────────────────────────────────────────────

@dataclass
class VideoFile:
    path:          str
    creation_time: Optional[datetime]   # UTC, from metadata or mtime
    duration:      float                # seconds

    @property
    def sort_key(self) -> float:
        if self.creation_time:
            return self.creation_time.timestamp()
        return os.path.getmtime(self.path)


def _ffprobe_creation_time(path: str) -> Optional[datetime]:
    """Extract creation_time from video metadata via ffprobe."""
    def _parse_dt(raw: str) -> Optional[datetime]:
        if not raw:
            return None
        txt = raw.strip()
        # Common ffprobe forms:
        # 2026-04-27T01:23:45.000000Z / 2026-04-27T01:23:45Z / 2026-04-27 01:23:45
        txt = txt.replace('Z', '+00:00')
        try:
            dt = datetime.fromisoformat(txt)
        except Exception:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    try:
        r = _run(['ffprobe', '-v', 'quiet', '-print_format', 'json',
             '-show_entries',
             'format=duration:'
             'format_tags=creation_time,com.apple.quicktime.creationdate:'
             'stream_tags=creation_time,com.apple.quicktime.creationdate',
             path], text=True, timeout=10)
        data = json.loads(r.stdout)
        fmt_tags = data.get('format', {}).get('tags', {}) or {}
        stream_tags = (data.get('streams', [{}])[0].get('tags', {}) if data.get('streams') else {}) or {}
        ct = (
            fmt_tags.get('creation_time')
            or fmt_tags.get('com.apple.quicktime.creationdate')
            or stream_tags.get('creation_time')
            or stream_tags.get('com.apple.quicktime.creationdate')
        )
        dur = float(data.get('format', {}).get('duration', 0))
        dt = _parse_dt(ct) if ct else None
        if dt is not None:
            return dt, dur
        return None, dur
    except Exception:
        logger.debug('ffprobe failed for %s', path, exc_info=True)
        return None, 0.0


def _sort_video_paths_by_metadata_only(paths: List[str]) -> List[str]:
    """Sort by ``creation_time`` / mtime−duration (same heuristics as ``scan_videos``)."""
    if len(paths) <= 1:
        return list(paths)

    def start_ts(path: str) -> float:
        got = _ffprobe_creation_time(path)
        if isinstance(got, tuple):
            dt, dur = got[0], float(got[1] or 0.0)
        else:
            dt, dur = None, 0.0
        if dt is not None:
            return dt.timestamp()
        mtime = os.path.getmtime(path)
        ct = datetime.fromtimestamp(mtime, tz=timezone.utc)
        if dur and dur > 0:
            ct = ct - timedelta(seconds=dur)
        return ct.timestamp()

    return sorted(paths, key=start_ts)


def sort_video_paths_by_start_time(paths: List[str]) -> List[str]:
    """Sort clips: prefer SMPTE timecode + frame continuity; else metadata (see ``video_clip_order``)."""
    from video_clip_order import sort_video_paths_with_timecode

    ordered, warnings = sort_video_paths_with_timecode(paths)
    for w in warnings:
        logger.info('video_clip_order: %s', w)
    return ordered


def scan_videos(folder: str, progress_cb: Optional[Callable[[str], None]] = None) -> List[VideoFile]:
    """Recursively scan a folder for video files."""
    all_paths = [
        os.path.join(root, fname)
        for root, _, files in os.walk(folder)
        for fname in sorted(files)
        if Path(fname).suffix in VIDEO_EXTENSIONS
    ]
    total = len(all_paths)

    results = []
    for i, path in enumerate(all_paths, 1):
        if progress_cb:
            progress_cb(f"Reading video metadata… ({i}/{total})  {os.path.basename(path)}")
        ct, dur = _ffprobe_creation_time(path)
        if ct is None:
            # Fallback: many cameras don't write creation_time reliably.
            # Use filesystem mtime (UTC) and back-project by duration so the
            # derived start time aligns with "record started" better than raw mtime.
            mtime = os.path.getmtime(path)
            ct = datetime.fromtimestamp(mtime, tz=timezone.utc)
            try:
                from datetime import timedelta
                if dur and dur > 0:
                    ct = ct - timedelta(seconds=float(dur))
            except Exception:
                pass
            logger.info('Video metadata missing creation_time, fallback to mtime: %s', path)
        results.append(VideoFile(path=path, creation_time=ct, duration=dur))
    results.sort(key=lambda v: v.sort_key)
    return results


# ── Video group ────────────────────────────────────────────────────────────────

@dataclass
class VideoGroup:
    """One or more consecutive video segments that form a single recording session."""
    files:      List[VideoFile]
    start_time: datetime      # UTC start of first segment
    end_time:   datetime      # UTC end of last segment
    total_dur:  float         # total duration in seconds

    @property
    def paths(self) -> List[str]:
        return [v.path for v in self.files]


def group_videos(videos: List[VideoFile]) -> List[VideoGroup]:
    """Group consecutive video segments (gap < MAX_GAP) into VideoGroups."""
    if not videos:
        return []
    groups: List[VideoGroup] = []
    cur: List[VideoFile] = [videos[0]]

    for v in videos[1:]:
        prev = cur[-1]
        prev_end = prev.creation_time.timestamp() + prev.duration if prev.creation_time else 0
        gap = v.sort_key - prev_end
        if abs(gap) <= MAX_GAP:
            cur.append(v)
        else:
            groups.append(_make_group(cur))
            cur = [v]
    groups.append(_make_group(cur))
    return groups


def _make_group(files: List[VideoFile]) -> VideoGroup:
    from datetime import timedelta
    start = files[0].creation_time
    total = sum(v.duration for v in files)
    end   = start + timedelta(seconds=total) if start else start
    return VideoGroup(files=files, start_time=start, end_time=end, total_dur=total)


# ── XRK conversion ─────────────────────────────────────────────────────────────

XRK_EXTENSIONS = {'.xrk', '.xrz', '.drk', '.XRK', '.XRZ', '.DRK'}


def convert_xrk_files(folder: str, progress_cb: Optional[Callable[[str], None]] = None) -> List[str]:
    """
    Walk *folder* for AIM XRK/XRZ/DRK files.  Any file that does not yet have
    a matching .csv alongside it is converted using xrk_to_csv.py.

    The AIM MatLabXRK DLL is downloaded automatically on first use (same logic
    as running xrk_to_csv.py from the command line).

    progress_cb(msg: str) is called with status strings if provided.
    Returns the list of CSV paths that were newly created.
    """
    import contextlib
    import io as _io

    pending = []
    for root, _, files in os.walk(folder):
        for fname in sorted(files):
            if Path(fname).suffix not in XRK_EXTENSIONS:
                continue
            xrk_path = os.path.join(root, fname)
            csv_path = os.path.splitext(xrk_path)[0] + '.csv'
            if not os.path.isfile(csv_path):
                pending.append((xrk_path, csv_path))
            else:
                # Regenerate if the existing CSV is missing the Lap column
                # (produced by an older version of xrk_to_csv.py)
                try:
                    with open(csv_path, 'r', encoding='utf-8-sig', errors='ignore') as _f:
                        line1 = _f.readline()
                        # Skip leading comment line (e.g. '# Session-Date: …')
                        header = _f.readline() if line1.startswith('#') else line1
                    if ',Lap,' not in header and not header.rstrip('\n').endswith(',Lap'):
                        os.remove(csv_path)
                        pending.append((xrk_path, csv_path))
                except OSError as e:
                    logger.warning('Could not process stale CSV %s: %s', csv_path, e)

    if not pending:
        return []

    import sys as _sys

    try:
        import xrk_to_csv as _xrk
    except ImportError:
        _xrk = None

    # Pick a reader: Windows DLL first (with auto-download), falling back to
    # libxrk (cross-platform PyPI package). We only invoke _find_dll() on
    # Windows because its auto-download path can open a Playwright browser
    # — useless on macOS/Linux where AIM ships no native binary anyway.
    dll_path = None
    if _xrk is not None and _sys.platform == 'win32':
        if progress_cb:
            progress_cb("Locating AIM MatLabXRK DLL…")
        try:
            dll_path = _xrk._find_dll()
        except SystemExit as e:
            if progress_cb:
                progress_cb(f"XRK DLL unavailable, falling back to libxrk: {e}")
        except Exception as e:
            if progress_cb:
                progress_cb(f"XRK DLL error, falling back to libxrk: {e}")

    libxrk_fn = None
    if not dll_path:
        try:
            from xrk_to_csv_libxrk import xrk_to_csv_libxrk as libxrk_fn
        except ImportError:
            if progress_cb:
                progress_cb(
                    "XRK reader unavailable — install libxrk (`pip install libxrk`) "
                    "or place a MatLabXRK DLL next to OpenLap (Windows only)."
                )
            return []

    new_csvs: List[str] = []
    for i, (xrk_path, csv_path) in enumerate(pending):
        fname = os.path.basename(xrk_path)
        if progress_cb:
            progress_cb(f"Converting {fname}  ({i + 1}/{len(pending)})…")
        try:
            buf = _io.StringIO()
            with contextlib.redirect_stdout(buf):
                if dll_path:
                    _xrk.xrk_to_csv(xrk_path, csv_path, dll_path)
                else:
                    libxrk_fn(xrk_path, csv_path)
            new_csvs.append(csv_path)
        except SystemExit as e:
            if progress_cb:
                progress_cb(f"  ✗ {fname}: {e}")
        except Exception as e:
            if progress_cb:
                progress_cb(f"  ✗ {fname}: {e}")

    return new_csvs


# ── CSV scanning ───────────────────────────────────────────────────────────────

def scan_csvs(folder: str) -> List[str]:
    """Recursively find all RaceBox, AIM, GPX, MoTeC .ld, and VBOX .vbo files."""
    import motec_data as _motec
    results = []
    for root, _, files in os.walk(folder):
        for fname in sorted(files):
            suffix = Path(fname).suffix
            path = os.path.join(root, fname)

            if suffix in VBO_EXTENSIONS:
                try:
                    with open(path, 'r', encoding='utf-8-sig', errors='ignore') as f:
                        head = f.read(256)
                    if '[header]' in head.lower():
                        results.append(path)
                except Exception:
                    logger.debug('Could not read VBO candidate %s', path, exc_info=True)
                continue

            if suffix in GPX_EXTENSIONS:
                # GPX files are identified by extension + quick content check
                try:
                    with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                        chunk = f.read(256)
                    if '<gpx' in chunk.lower():
                        results.append(path)
                except Exception:
                    logger.debug('Could not read GPX candidate %s', path, exc_info=True)
                continue

            if suffix in LD_EXTENSIONS:
                if _motec.is_motec_ld(path):
                    results.append(path)
                continue

            if suffix not in CSV_EXTENSIONS:
                continue

            try:
                with open(path, 'r', encoding='utf-8-sig', errors='ignore') as f:
                    content = f.read(2000)
                if 'Record,Time,' in content and 'RaceBox' in content:
                    results.append(path)
                elif content.startswith('Time (s),') or '\nTime (s),' in content:
                    results.append(path)
            except Exception:
                logger.debug('Could not read CSV candidate %s', path, exc_info=True)
    return results


# ── Matching ───────────────────────────────────────────────────────────────────

@dataclass
class MatchedSession:
    csv_path:         str
    video_group:      Optional[VideoGroup]
    time_delta:       float            # seconds between CSV start and video group start
    csv_start:        Optional[datetime]
    video_start:      Optional[datetime]
    matched:          bool             # True if within MATCH_WINDOW
    source:           str  = 'RaceBox' # 'RaceBox' | 'AIM'
    needs_conversion: bool = False     # True for XRK files not yet converted to CSV
    xrk_path:         Optional[str] = None  # source XRK path when needs_conversion=True


def scan_pending_xrk(folder: str) -> List[Tuple[str, str]]:
    """Return (xrk_path, future_csv_path) for XRK files that have no matching CSV yet."""
    results = []
    for root, _, files in os.walk(folder):
        for fname in sorted(files):
            if Path(fname).suffix not in XRK_EXTENSIONS:
                continue
            xrk_path = os.path.join(root, fname)
            csv_path = os.path.splitext(xrk_path)[0] + '.csv'
            if not os.path.isfile(csv_path):
                results.append((xrk_path, csv_path))
    return results


def _csv_source(path: str) -> str:
    """Quick peek at a file to determine its data source."""
    suffix = Path(path).suffix.lower()
    if suffix == '.vbo':
        return 'VBOX'
    if suffix == '.gpx':
        return 'GPX'
    if suffix == '.ld':
        return 'MoTeC'
    try:
        with open(path, 'r', encoding='utf-8-sig', errors='ignore') as f:
            head = f.read(300)
        if head.startswith('Time (s),') or '\nTime (s),' in head:
            return 'AIM'
    except Exception:
        pass
    return 'RaceBox'


def _tail_file_text(path: str, max_bytes: int = 262_144) -> str:
    """Read up to *max_bytes* from the end of a text file (UTF-8 with BOM tolerance)."""
    try:
        size = os.path.getsize(path)
        with open(path, 'rb') as f:
            if size <= max_bytes:
                raw = f.read().decode('utf-8-sig', errors='ignore')
            else:
                f.seek(max(0, size - max_bytes))
                raw = f.read().decode('utf-8-sig', errors='ignore')
                if '\n' in raw:
                    raw = raw.split('\n', 1)[1]
            return raw
    except OSError:
        return ''


def _read_csv_session_end(path: str, csv_start: Optional[datetime]) -> Optional[datetime]:
    """Best-effort session end (UTC) from file tail. ``csv_start`` required for AIM / elapsed CSV."""
    if not csv_start:
        return None
    suf = Path(path).suffix.lower()
    if suf == '.gpx':
        import re as _re
        tail = _tail_file_text(path, 512_000)
        times = _re.findall(r'<time>([^<]+)</time>', tail)
        if not times:
            return None
        raw = times[-1].strip().replace('Z', '+00:00')
        try:
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            return None

    if suf != '.csv':
        return None

    tail = _tail_file_text(path)
    if not tail.strip():
        return None

    # AIM: last row's Time (s) + ``# Session-Date`` anchor (same as ``_read_csv_start_time``).
    if _csv_source(path) == 'AIM':
        lines = [ln.strip() for ln in tail.splitlines() if ln.strip()]
        for ln in reversed(lines):
            if ln.startswith('#') or ln.startswith('Time (s),'):
                continue
            if ',' not in ln:
                continue
            cell0 = ln.split(',', 1)[0].strip()
            try:
                sec = float(cell0)
            except ValueError:
                continue
            return csv_start + timedelta(seconds=sec)
        return None

    # RaceBox: last ``Record,`` row — Time column is ISO or seconds-from-start
    if 'Record,Time,' not in tail:
        return None
    lines = [ln.strip() for ln in tail.splitlines() if ln.strip()]
    for ln in reversed(lines):
        if not ln.startswith(tuple('0123456789')):
            continue
        parts = ln.split(',')
        if len(parts) < 2:
            continue
        if not parts[0].strip().isdigit():
            continue
        ts = parts[1].strip()
        if 'T' in ts:
            ts2 = ts.replace('Z', '+00:00')
            try:
                dt = datetime.fromisoformat(ts2)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except ValueError:
                continue
        try:
            elapsed = float(ts)
            return csv_start + timedelta(seconds=elapsed)
        except ValueError:
            continue
    return None


def _interval_overlap_seconds(
    a0: datetime, a1: datetime, b0: datetime, b1: datetime,
) -> float:
    """Length of intersection of two closed intervals in seconds (0 if none)."""
    lo = max(a0, b0)
    hi = min(a1, b1)
    if hi <= lo:
        return 0.0
    return (hi - lo).total_seconds()


def match_sessions(
    csv_paths: List[str],
    video_groups: List[VideoGroup],
) -> List[MatchedSession]:
    """
    Match each CSV to a video group: prefer **time-range overlap** (telemetry vs
    grouped video wall-clock span); else closest video-group start within
    ``MATCH_WINDOW``.
    """
    results = []
    for csv_path in csv_paths:
        try:
            csv_start = _read_csv_start_time(csv_path)
        except Exception:
            logger.debug('Could not read start time from %s', csv_path, exc_info=True)
            csv_start = None

        csv_end = _read_csv_session_end(csv_path, csv_start) if csv_start else None

        best_group: Optional[VideoGroup] = None
        best_delta = float('inf')
        best_vstart: Optional[datetime] = None
        matched = False

        if csv_start and video_groups:
            # ── 1) Overlap-based (needs session end) ─────────────────────────
            if csv_end and csv_end > csv_start:
                scored: List[Tuple[float, VideoGroup]] = []
                for grp in video_groups:
                    if not grp.start_time or not grp.end_time:
                        continue
                    ov = _interval_overlap_seconds(csv_start, csv_end, grp.start_time, grp.end_time)
                    if ov > 0:
                        scored.append((ov, grp))
                if scored:
                    scored.sort(
                        key=lambda t: (-t[0], abs((csv_start - t[1].start_time).total_seconds()))
                    )
                    best_group = scored[0][1]
                    best_vstart = best_group.start_time
                    best_delta = abs((csv_start - best_vstart).total_seconds())
                    matched = True

            # ── 2) Legacy: closest group start within MATCH_WINDOW ─────────────
            if not matched:
                for grp in video_groups:
                    if not grp.start_time:
                        continue
                    delta = abs((csv_start - grp.start_time).total_seconds())
                    if delta < best_delta:
                        best_delta = delta
                        best_group = grp
                        best_vstart = grp.start_time
                matched = bool(best_group and best_delta <= MATCH_WINDOW)

        results.append(MatchedSession(
            csv_path    = csv_path,
            video_group = best_group if matched else None,
            time_delta  = best_delta,
            csv_start   = csv_start,
            video_start = best_vstart,
            matched     = matched,
            source      = _csv_source(csv_path),
        ))

    # Sort by CSV start time
    results.sort(key=lambda m: m.csv_start.timestamp() if m.csv_start else 0)
    return results


def _read_csv_start_time(path: str) -> Optional[datetime]:
    """Read session start time from a data file.

    VBOX:    reads absolute time from "File created on/in ..." in file content.
    GPX:     reads the first <time> element.
    RaceBox: reads the 'Date UTC,' metadata line.
    AIM:     reads the '# Session-Date:' comment or falls back to mtime.
    MoTeC:   reads the date/time fields from the binary header.
    """
    if Path(path).suffix.lower() == '.vbo':
        try:
            import re as _re
            with open(path, 'r', encoding='utf-8-sig', errors='ignore') as f:
                head = f.read(64 * 1024)
            m = _re.search(
                r'file\s+created\s+(?:on|in)\s+(\d{2})/(\d{2})/(\d{4})(?:\s+at\s+(\d{2}):(\d{2}):(\d{2}))?',
                head,
                flags=_re.IGNORECASE,
            )
            if m:
                day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
                # Prefer the first data row's HHMMSS.SS for sub-second precision.
                # Many VBO exports store the header timestamp in local time, but the
                # [data] time channel as UTC HHMMSS.SS. We treat [data] time as UTC,
                # and choose the correct UTC date (±1 day) so it's closest to the
                # header timestamp converted to UTC.
                hdr_h = int(m.group(4) or 0)
                hdr_m = int(m.group(5) or 0)
                hdr_s = int(m.group(6) or 0)
                hh, mm, ss, ms = hdr_h, hdr_m, hdr_s, 0
                try:
                    dm = _re.search(r'^\s*\[data\]\s*$(?:\r?\n)+([^\r\n]+)', head, flags=_re.IGNORECASE | _re.MULTILINE)
                    if dm:
                        cols = dm.group(1).split()
                        if len(cols) >= 2:
                            raw = float(cols[1])  # time column usually second token
                            h2 = int(raw) // 10000
                            m2 = (int(raw) // 100) % 100
                            s2 = raw - h2 * 10000 - m2 * 100
                            hh, mm, ss = int(h2), int(m2), int(s2)
                            ms = int(round((s2 % 1) * 1000))
                except Exception:
                    pass

                local_tz = datetime.now().astimezone().tzinfo or timezone.utc
                header_local = datetime(year, month, day, hdr_h, hdr_m, hdr_s, tzinfo=local_tz)
                header_utc = header_local.astimezone(timezone.utc)

                base_date = datetime(year, month, day, tzinfo=timezone.utc)
                candidates = []
                for dday in (-1, 0, 1):
                    dt = (base_date + timedelta(days=dday)).replace(
                        hour=hh, minute=mm, second=ss, microsecond=ms * 1000
                    )
                    candidates.append(dt)
                best = min(candidates, key=lambda dt: abs((dt - header_utc).total_seconds()))
                return best
        except Exception:
            logger.debug('Could not read VBOX start time from %s', path, exc_info=True)
        return None

    if Path(path).suffix.lower() == '.ld':
        import struct as _s
        try:
            with open(path, 'rb') as f:
                hdr = f.read(0x90)
            date_str = hdr[0x5E:0x68].split(b'\x00')[0].decode('ascii', errors='replace').strip()
            time_str = hdr[0x7E:0x86].split(b'\x00')[0].decode('ascii', errors='replace').strip()
            dt = datetime.strptime(f"{date_str} {time_str}", "%d/%m/%Y %H:%M:%S")
            return dt.replace(tzinfo=timezone.utc)
        except Exception:
            pass
        mtime = os.path.getmtime(path)
        return datetime.fromtimestamp(mtime, tz=timezone.utc)

    if Path(path).suffix.lower() == '.gpx':
        import re
        try:
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read(4096)
            m = re.search(r'<time>([^<]+)</time>', content)
            if m:
                val = m.group(1).strip().replace('Z', '+00:00')
                dt = datetime.fromisoformat(val)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
        except Exception:
            pass
        mtime = os.path.getmtime(path)
        return datetime.fromtimestamp(mtime, tz=timezone.utc)

    with open(path, 'r', encoding='utf-8-sig', errors='ignore') as f:
        for line in f:
            # AIM CSV: embedded session date comment
            if line.startswith('# Session-Date:'):
                val = line.split(':', 1)[1].strip()
                val = val.replace('Z', '+00:00')
                try:
                    dt = datetime.fromisoformat(val)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return dt
                except ValueError:
                    break
            # RaceBox CSV: Date UTC metadata line
            if line.startswith('Date UTC,'):
                val = line.strip().split(',', 1)[1].strip()
                val = val.replace('Z', '+00:00')
                dt  = datetime.fromisoformat(val)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            if line.startswith('Record,Time,') or line.startswith('Time (s),'):
                break  # past header, not found

    # Fallback: file mtime
    mtime = os.path.getmtime(path)
    return datetime.fromtimestamp(mtime, tz=timezone.utc)


# ── Batch state ────────────────────────────────────────────────────────────────

@dataclass
class SessionState:
    csv_path:     str
    video_paths:  List[str]
    sync_offset:  Optional[float]   # None = not yet synced
    status:       str               # 'pending' | 'synced' | 'rendering' | 'done' | 'error'
    output_files: List[str]         = field(default_factory=list)
    error_msg:    str               = ''
    lap_mode:     str               = 'fastest'  # 'all' | 'fastest' | 'selection'
    selected_laps: List[int]        = field(default_factory=list)


@dataclass
class BatchState:
    output_dir:  str
    sessions:    List[SessionState] = field(default_factory=list)
    created_at:  str = ''
    version:     int = 2

    def save(self, path: str) -> None:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(asdict(self), f, indent=2, default=str)

    @staticmethod
    def load(path: str) -> 'BatchState':
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        sessions = [SessionState(**s) for s in data.get('sessions', [])]
        return BatchState(
            output_dir  = data.get('output_dir', ''),
            sessions    = sessions,
            created_at  = data.get('created_at', ''),
            version     = data.get('version', 1),
        )

    def get_session(self, csv_path: str) -> Optional[SessionState]:
        return next((s for s in self.sessions if s.csv_path == csv_path), None)

    def upsert_session(self, sess: SessionState) -> None:
        for i, s in enumerate(self.sessions):
            if s.csv_path == sess.csv_path:
                self.sessions[i] = sess
                return
        self.sessions.append(sess)

    @property
    def pending(self) -> List[SessionState]:
        return [s for s in self.sessions if s.status in ('pending', 'synced')]

    @property
    def done(self) -> List[SessionState]:
        return [s for s in self.sessions if s.status == 'done']


def build_batch_state(matches: List[MatchedSession],
                      output_dir: str) -> BatchState:
    """Create a fresh BatchState from matched sessions."""
    state = BatchState(
        output_dir  = output_dir,
        created_at  = datetime.now(tz=timezone.utc).isoformat(),
    )
    for m in matches:
        if not m.matched:
            continue
        ss = SessionState(
            csv_path    = m.csv_path,
            video_paths = m.video_group.paths if m.video_group else [],
            sync_offset = None,
            status      = 'pending',
        )
        state.sessions.append(ss)
    return state
