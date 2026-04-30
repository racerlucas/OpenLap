"""
webview_api.py — Python API exposed to JavaScript via window.pywebview.api.

All public methods are called by JS with await window.pywebview.api.method(args).
Return values must be JSON-serialisable.
Push-events (export progress, scan updates) are sent via window.evaluate_js().

Preview vs export — consistency boundary
-----------------------------------------
**Unify:** anything that defines *which samples or numbers* appear in overlay
preview vs exported video — session load, sync offset, ``sample_session_points`` /
``build_history_row``, delta series (``compute_preview_delta`` / export delta),
map tracks (``get_preview_map_tracks`` / ``video_renderer``), lap-info fields,
channel metadata (``gauge_channels``, ``get_channel_meta``, ``get_editor_catalog``).

**Separate:** RPC names, push payloads, UI-only state, Canvas vs matplotlib
*drawing* — decouple when it does not risk preview/export drift. See
``telemetry_algorithms.py`` module docstring.
"""
from __future__ import annotations

import http.server
import json
import logging
import mimetypes
import os
import threading
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path
from typing import Optional

import webview

from app_config import AppConfig, overlay_from_dict, load_scan_cache

logger = logging.getLogger(__name__)

_ALLOWED_VIDEO_EXTENSIONS = frozenset({
    '.mp4', '.mov', '.avi', '.mkv', '.m4v',
    '.MP4', '.MOV', '.AVI', '.MKV', '.M4V',
})


class _VideoFileHandler(http.server.BaseHTTPRequestHandler):
    """Minimal HTTP handler that serves arbitrary local files with range support.

    The URL path is the absolute file path with forward slashes, e.g.
    /C:/Videos/race.mp4  → opens C:/Videos/race.mp4 on Windows.
    """

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        if 'f' in params:
            # Path delivered as ?f=<url-encoded Windows path> — no slash mangling
            raw = params['f'][0]
        else:
            # Legacy fallback: path embedded in URL path (only works for local C:/ paths)
            raw = urllib.parse.unquote(parsed.path)
            if raw.startswith('/') and len(raw) > 2 and raw[2] == ':':
                raw = raw[1:]

        # Security: only serve recognised video extensions to prevent path traversal
        ext = os.path.splitext(raw)[1]
        if ext not in _ALLOWED_VIDEO_EXTENSIONS:
            logger.warning('VideoServer 403: disallowed extension %s for %s', ext, raw)
            self.send_error(403, 'Forbidden')
            return

        logger.debug('VideoServer GET %s → %s (exists=%s)', self.path, raw, os.path.isfile(raw))
        if not os.path.isfile(raw):
            logger.warning('VideoServer 404: %s', raw)
            self.send_error(404, 'File not found')
            return
        size  = os.path.getsize(raw)
        mime  = mimetypes.guess_type(raw)[0] or 'application/octet-stream'
        rng   = self.headers.get('Range', '')
        if rng:
            try:
                parts = rng.replace('bytes=', '').split('-')
                start = int(parts[0]) if parts[0] else 0
                end   = int(parts[1]) if parts[1] else size - 1
            except (ValueError, IndexError):
                self.send_error(400, 'Invalid Range header')
                return
            end = min(end, size - 1)
            if start < 0 or start > end or start >= size:
                self.send_error(416, 'Range Not Satisfiable')
                return
            length = end - start + 1
            self.send_response(206)
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        else:
            start, end, length = 0, size - 1, size
            self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(length))
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        try:
            with open(raw, 'rb') as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            # Browser/video element may cancel range requests aggressively during
            # seek/tab switch; treat these socket aborts as normal.
            pass

    def log_message(self, *args):
        pass  # suppress server logs


class WebviewAPI:
    """
    One instance of this class is created in main.py and passed to
    webview.create_window(js_api=api).  Every public method becomes
    callable from JavaScript as: await window.pywebview.api.<method>(...)
    """

    def __init__(self):
        self._config: AppConfig = AppConfig.load()
        self._window: Optional[webview.Window] = None
        self._export_cancel    = threading.Event()
        self._export_thread:   Optional[threading.Thread] = None
        self._rb_cancel        = threading.Event()
        self._rb_thread:       Optional[threading.Thread] = None
        self._auto_sync_cancel = threading.Event()
        self._auto_sync_thread: Optional[threading.Thread] = None
        self._auto_sync_cfg_lock = threading.Lock()
        self._thread_lock      = threading.Lock()
        # Queues (FIFO). Offsets in config are never cleared here — only work is deferred.
        self._export_queue: list = []
        self._auto_sync_batch_queue: list = []
        self._decode_sessions: dict[str, object] = {}
        self._decode_lock = threading.Lock()
        # Cache FFmpeg nullsrc encoder probes (~10 calls); refresh periodically.
        self._encoder_probe_cache: Optional[tuple] = None  # (monotonic_ts, dict[str, bool])

    # ── Called by main.py once the window is ready ────────────────────────────
    def set_window(self, window: webview.Window) -> None:
        self._window = window

    def _push(self, event_type: str, **payload) -> None:
        """Push a CustomEvent to JavaScript."""
        if self._window is None:
            return
        import json
        detail = json.dumps({'type': event_type, **payload})
        # Escape single quotes in detail for safe JS injection
        detail_escaped = detail.replace('\\', '\\\\').replace("'", "\\'")
        self._window.evaluate_js(
            f"window.dispatchEvent(new CustomEvent('openlap', {{detail: JSON.parse('{detail_escaped}')}}));"
        )

    # ── Video file server ─────────────────────────────────────────────────────
    def get_video_server_port(self) -> int:
        """Return the localhost port of the video file server, starting it if needed."""
        if hasattr(self, '_video_port'):
            return self._video_port
        try:
            server = http.server.HTTPServer(('127.0.0.1', 0), _VideoFileHandler)
            self._video_port = server.server_address[1]
            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()
            logger.info('Video file server started on port %d', self._video_port)
        except Exception:
            logger.exception('Failed to start video file server')
            self._video_port = 0
        return self._video_port

    # ── Video decode helpers (Data tab sync panel) ───────────────────────────
    def get_video_fps(self, video_path: str) -> dict:
        """Return basic video timing info for sync UI."""
        try:
            import cv2
            cap = cv2.VideoCapture(video_path)
            if not cap.isOpened():
                return {'ok': False, 'error': 'cannot open video'}
            fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
            frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
            cap.release()
            return {
                'ok': True,
                'fps': fps,
                'frame_count': frame_count,
                'duration': (frame_count / fps) if fps > 0 else 0.0,
                'width': width,
                'height': height,
            }
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def get_video_probe(self, video_path_or_paths) -> dict:
        """Probe a video with ffprobe (preferred) and return container + stream info.

        Returns:
          {ok, path, extension, container, width, height, fps, duration, has_audio}
        """
        try:
            # Accept either a single path string or an array (use first entry).
            if isinstance(video_path_or_paths, (list, tuple)):
                video_path = str(video_path_or_paths[0]) if video_path_or_paths else ''
            else:
                video_path = str(video_path_or_paths or '')

            if not video_path:
                return {'ok': False, 'error': 'no video path'}
            if not os.path.exists(video_path):
                return {'ok': False, 'error': 'video not found'}

            ext = os.path.splitext(video_path)[1].lower()
            container = ext[1:] if ext.startswith('.') else ext

            # ffprobe gives authoritative stream fps and duration even for VFR.
            from utils import _run

            cmd = [
                'ffprobe', '-v', 'quiet', '-print_format', 'json',
                '-show_format', '-show_streams',
                video_path,
            ]
            r = _run(cmd, capture_output=True, text=True, timeout=10)
            if r.returncode == 0 and r.stdout:
                data = json.loads(r.stdout or '{}') if r.stdout else {}
                streams = data.get('streams') or []
                fmt = data.get('format') or {}

                v0 = next((s for s in streams if str(s.get('codec_type')) == 'video'), None)
                a0 = next((s for s in streams if str(s.get('codec_type')) == 'audio'), None)
                has_audio = a0 is not None
                audio_bitrate_kbps = 0
                if a0:
                    try:
                        br = int(a0.get('bit_rate') or 0)
                        if br > 0:
                            audio_bitrate_kbps = max(32, min(320, br // 1000))
                    except (TypeError, ValueError):
                        audio_bitrate_kbps = 0

                width = int((v0 or {}).get('width') or 0)
                height = int((v0 or {}).get('height') or 0)

                # Prefer avg_frame_rate; fall back to r_frame_rate.
                fps = 0.0
                for key in ('avg_frame_rate', 'r_frame_rate'):
                    fr = (v0 or {}).get(key)
                    if fr and isinstance(fr, str) and '/' in fr:
                        try:
                            n, d = fr.split('/', 1)
                            n, d = float(n), float(d)
                            if d:
                                fps = n / d
                                if fps > 0:
                                    break
                        except Exception:
                            pass

                duration = 0.0
                try:
                    duration = float(fmt.get('duration') or 0.0)
                except Exception:
                    duration = 0.0
                if duration <= 0 and v0 is not None:
                    try:
                        duration = float(v0.get('duration') or 0.0)
                    except Exception:
                        duration = 0.0

                # Prefer format_name (ffprobe), but keep it short/stable.
                fmt_name = str(fmt.get('format_name') or '').split(',')[0].strip()
                if fmt_name:
                    container = fmt_name

                return {
                    'ok': True,
                    'path': os.path.abspath(video_path),
                    'extension': ext,
                    'container': container,
                    'width': width,
                    'height': height,
                    'fps': fps,
                    'duration': duration,
                    'has_audio': has_audio,
                    'audio_bitrate_kbps': int(audio_bitrate_kbps),
                }

            # Fallback: OpenCV probe (same as get_video_fps).
            probe = self.get_video_fps(video_path)
            if isinstance(probe, dict) and probe.get('ok'):
                return {
                    'ok': True,
                    'path': os.path.abspath(video_path),
                    'extension': ext,
                    'container': container,
                    'width': int(probe.get('width') or 0),
                    'height': int(probe.get('height') or 0),
                    'fps': float(probe.get('fps') or 0.0),
                    'duration': float(probe.get('duration') or 0.0),
                    'has_audio': True,  # unknown via OpenCV; assume yes
                }
            return {'ok': False, 'error': 'probe failed'}
        except Exception as e:
            logger.exception('get_video_probe failed')
            return {'ok': False, 'error': str(e)}

    def _decode_frame_b64(self, cap, frame_idx: int) -> tuple[bool, str]:
        import cv2
        cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, int(frame_idx)))
        ok, frame = cap.read()
        if not ok or frame is None:
            return False, ''
        ok_jpg, buf = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        if not ok_jpg:
            return False, ''
        import base64
        return True, base64.b64encode(buf.tobytes()).decode('ascii')

    def decode_video_frame(self, video_path: str, frame_idx: int = None, time_sec: float = None) -> dict:
        """Decode one frame directly (stateless helper)."""
        try:
            import cv2
            cap = cv2.VideoCapture(video_path)
            if not cap.isOpened():
                return {'ok': False, 'error': 'cannot open video'}
            fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
            frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            if frame_idx is None and time_sec is not None and fps > 0:
                frame_idx = int(max(0.0, float(time_sec)) * fps)
            idx = int(frame_idx or 0)
            idx = max(0, min(max(0, frame_count - 1), idx))
            ok, image_b64 = self._decode_frame_b64(cap, idx)
            cap.release()
            if not ok:
                return {'ok': False, 'error': 'decode failed'}
            return {'ok': True, 'fps': fps, 'frame_count': frame_count, 'frame_idx': idx, 'image_b64': image_b64}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def open_decode_session(self, video_path: str, cache_radius: int = 0) -> dict:
        """Open persistent decode session for responsive frame stepping."""
        try:
            import cv2
            cap = cv2.VideoCapture(video_path)
            if not cap.isOpened():
                return {'ok': False, 'error': 'cannot open video'}
            fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
            frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            import uuid
            sid = uuid.uuid4().hex
            with self._decode_lock:
                self._decode_sessions[sid] = {
                    'cap': cap,
                    'fps': fps,
                    'frame_count': frame_count,
                    'video_path': video_path,
                    'cache_radius': int(cache_radius or 0),
                    'last_frame': 0,
                }
            return {'ok': True, 'session_id': sid, 'fps': fps, 'frame_count': frame_count}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def close_decode_session(self, session_id: str) -> dict:
        # Hold lock through release so no seek runs on this cap mid-teardown (Windows
        # libavcodec pthread_frame async_lock asserts on concurrent cap access).
        with self._decode_lock:
            sess = self._decode_sessions.pop(str(session_id or ''), None)
            if not sess:
                return {'ok': True}
            try:
                cap = sess.get('cap')
                if cap is not None:
                    cap.release()
            except Exception:
                pass
        return {'ok': True}

    def _decode_session_seek_locked(self, sess: dict, frame_idx: int = None, time_sec: float = None) -> dict:
        """Decode one frame from an open session. Caller must hold ``_decode_lock``."""
        cap = sess['cap']
        fps = float(sess.get('fps') or 0.0)
        frame_count = int(sess.get('frame_count') or 0)
        if frame_idx is None and time_sec is not None and fps > 0:
            frame_idx = int(max(0.0, float(time_sec)) * fps)
        idx = int(frame_idx or 0)
        idx = max(0, min(max(0, frame_count - 1), idx))
        ok, image_b64 = self._decode_frame_b64(cap, idx)
        if not ok:
            return {'ok': False, 'error': 'decode failed'}
        sess['last_frame'] = idx
        return {'ok': True, 'fps': fps, 'frame_count': frame_count, 'frame_idx': idx, 'image_b64': image_b64}

    def decode_session_seek(self, session_id: str, frame_idx: int = None, time_sec: float = None) -> dict:
        with self._decode_lock:
            sess = self._decode_sessions.get(str(session_id or ''))
            if not sess:
                return {'ok': False, 'error': 'decode session not found'}
            try:
                return self._decode_session_seek_locked(sess, frame_idx, time_sec)
            except Exception as e:
                return {'ok': False, 'error': str(e)}

    def debug_sync_seek(self, tag: str = '', details: dict = None) -> dict:
        """Data-tab sync UI seek hook (silenced)."""
        return {'ok': True}

    def decode_session_step(self, session_id: str, direction: int = 1) -> dict:
        with self._decode_lock:
            sess = self._decode_sessions.get(str(session_id or ''))
            if not sess:
                return {'ok': False, 'error': 'decode session not found'}
            cur = int(sess.get('last_frame') or 0)
            step = 1 if int(direction or 1) >= 0 else -1
            try:
                return self._decode_session_seek_locked(sess, frame_idx=cur + step, time_sec=None)
            except Exception as e:
                return {'ok': False, 'error': str(e)}

    def step_video_frame(self, video_path: str, current_time: float, direction: int) -> dict:
        """Stateless step helper used by older UI paths."""
        info = self.get_video_fps(video_path)
        if not info.get('ok'):
            return info
        fps = float(info.get('fps') or 0.0)
        if fps <= 0:
            return {'ok': False, 'error': 'invalid fps'}
        cur_idx = int(max(0.0, float(current_time or 0.0)) * fps)
        step = 1 if int(direction or 1) >= 0 else -1
        return self.decode_video_frame(video_path, frame_idx=cur_idx + step, time_sec=None)

    # ── Config ────────────────────────────────────────────────────────────────
    def get_config(self) -> dict:
        cfg = asdict(self._config)
        # Inject the helper method result as a plain list
        cfg['all_telemetry_paths'] = self._config.all_telemetry_paths()
        return cfg

    def _export_encoder_probe_dict(self) -> dict:
        import time
        now = time.monotonic()
        c = getattr(self, '_encoder_probe_cache', None)
        if c and (now - c[0]) < 90.0:
            return c[1]
        from export_encoder import probe_encoder_availability
        d = probe_encoder_availability()
        self._encoder_probe_cache = (now, d)
        return d

    def _sync_resolved_export_encoder(self) -> None:
        from export_encoder import resolve_export_encoder
        self._config.encoder = resolve_export_encoder(
            getattr(self._config, 'export_video_codec', 'h264'),
            getattr(self._config, 'export_encoder_family', 'auto'),
            self._export_encoder_probe_dict(),
        )

    def _resolved_export_encoder(self, params: Optional[dict]) -> str:
        from export_encoder import resolve_export_encoder
        cfg = self._config
        p = params if isinstance(params, dict) else {}
        c = str(p.get('export_video_codec') or getattr(cfg, 'export_video_codec', 'h264') or 'h264')
        f = str(p.get('export_encoder_family') or getattr(cfg, 'export_encoder_family', 'auto') or 'auto')
        return resolve_export_encoder(c, f, self._export_encoder_probe_dict())

    def save_config(self, data: dict) -> None:
        # Update string fields
        simple_fields = [
            'racebox_path', 'aim_path', 'motec_path', 'gpx_path', 'vbox_path',
            'telemetry_path', 'video_path', 'export_path', 'racebox_email',
        ]
        for f in simple_fields:
            if f in data:
                setattr(self._config, f, data[f])
        if 'export_video_codec' in data:
            v = str(data.get('export_video_codec') or '').strip().lower()
            if v in ('h264', 'h265', 'av1'):
                self._config.export_video_codec = v
        if 'export_encoder_family' in data:
            v = str(data.get('export_encoder_family') or '').strip().lower()
            if v in ('auto', 'cpu', 'nvenc', 'amf', 'qsv', 'videotoolbox'):
                self._config.export_encoder_family = v
        if 'encoder' in data:
            self._config.encoder = str(data['encoder'])
        if 'crf' in data:
            self._config.crf = int(data['crf'])
        if 'workers' in data:
            self._config.workers = int(data['workers'])
        if 'export_rate_mode' in data:
            self._config.export_rate_mode = str(data['export_rate_mode']).lower()
        if 'export_video_bitrate_kbps' in data:
            try:
                self._config.export_video_bitrate_kbps = max(0, int(data['export_video_bitrate_kbps']))
            except (TypeError, ValueError):
                pass
        if 'export_video_max_bitrate_kbps' in data:
            try:
                self._config.export_video_max_bitrate_kbps = max(0, int(data['export_video_max_bitrate_kbps']))
            except (TypeError, ValueError):
                pass
        if 'export_audio_bitrate_kbps' in data:
            try:
                self._config.export_audio_bitrate_kbps = max(0, min(320, int(data['export_audio_bitrate_kbps'])))
            except (TypeError, ValueError):
                pass
        if 'export_two_pass' in data:
            self._config.export_two_pass = bool(data['export_two_pass'])
        if 'export_max_quality' in data:
            self._config.export_max_quality = bool(data['export_max_quality'])
        if 'export_container_choice' in data:
            self._config.export_container_choice = str(data['export_container_choice'] or 'match_source').strip()
        if 'export_target_res' in data:
            self._config.export_target_res = str(data.get('export_target_res') or 'auto').strip() or 'auto'
        if 'export_target_fps' in data:
            self._config.export_target_fps = str(data.get('export_target_fps') or 'auto').strip() or 'auto'
        if 'export_scope' in data:
            v = str(data.get('export_scope') or 'full').strip()
            if v in (
                'selected_lap', 'lap_range', 'fastest_lap', 'all_laps', 'full', 'clip',
            ):
                self._config.export_scope = v
        if 'export_padding' in data:
            try:
                p = float(data['export_padding'])
                self._config.export_padding = max(0.0, min(120.0, p))
            except (TypeError, ValueError):
                pass
        if 'export_clip_start_s' in data:
            try:
                self._config.export_clip_start_s = max(0.0, float(data['export_clip_start_s'] or 0.0))
            except (TypeError, ValueError):
                pass
        if 'export_clip_end_s' in data:
            try:
                self._config.export_clip_end_s = max(0.0, float(data['export_clip_end_s'] or 0.0))
            except (TypeError, ValueError):
                pass
        if 'export_overlay_only' in data:
            self._config.export_overlay_only = bool(data['export_overlay_only'])
        if 'export_lap_range_start' in data:
            try:
                self._config.export_lap_range_start = max(1, int(data['export_lap_range_start']))
            except (TypeError, ValueError):
                pass
        if 'export_lap_range_end' in data:
            raw = data['export_lap_range_end']
            if raw is None or (isinstance(raw, str) and not str(raw).strip()):
                self._config.export_lap_range_end = None
            else:
                try:
                    self._config.export_lap_range_end = int(raw)
                except (TypeError, ValueError):
                    self._config.export_lap_range_end = None
        # Merge dict fields (JS may send partial updates)
        if 'offsets' in data and isinstance(data['offsets'], dict):
            self._config.offsets.update(data['offsets'])
        if 'offset_sources' in data and isinstance(data['offset_sources'], dict):
            self._config.offset_sources.update(data['offset_sources'])
        if 'bike_overrides' in data and isinstance(data['bike_overrides'], dict):
            self._config.bike_overrides.update(data['bike_overrides'])
        if 'auto_sync_enabled' in data:
            self._config.auto_sync_enabled = bool(data['auto_sync_enabled'])
        if 'auto_sync_workers' in data:
            try:
                self._config.auto_sync_workers = max(
                    1, min(8, int(data['auto_sync_workers'])),
                )
            except (TypeError, ValueError):
                pass
        if 'auto_sync_use_motion' in data:
            self._config.auto_sync_use_motion = bool(data['auto_sync_use_motion'])
        try:
            self._sync_resolved_export_encoder()
        except Exception:
            logger.debug('sync resolved export encoder failed', exc_info=True)
        self._config.save()

    def _export_encode_options(self, params: Optional[dict]) -> dict:
        """Merge RPC params with persisted config for FFmpeg ``encode_options``."""
        cfg = self._config
        p = params if isinstance(params, dict) else {}

        def pick_int(key: str, default: int = 0,
                     lo: Optional[int] = None, hi: Optional[int] = None) -> int:
            v = p.get(key)
            if v is None:
                v = getattr(cfg, key, default)
            try:
                n = int(v)
            except (TypeError, ValueError):
                n = default
            if lo is not None:
                n = max(lo, n)
            if hi is not None:
                n = min(hi, n)
            return n

        rm = p.get('export_rate_mode')
        if rm is None:
            rm = getattr(cfg, 'export_rate_mode', 'cq')
        tp = p.get('export_two_pass')
        if tp is None:
            tp = getattr(cfg, 'export_two_pass', False)
        mq = p.get('export_max_quality')
        if mq is None:
            mq = getattr(cfg, 'export_max_quality', False)
        tr = p.get('export_target_res')
        if tr is None:
            tr = getattr(cfg, 'export_target_res', 'auto')
        tf = p.get('export_target_fps')
        if tf is None:
            tf = getattr(cfg, 'export_target_fps', 'auto')

        return {
            'export_rate_mode': str(rm or 'cq').lower(),
            'export_video_bitrate_kbps': pick_int('export_video_bitrate_kbps', 0, lo=0),
            'export_video_max_bitrate_kbps': pick_int('export_video_max_bitrate_kbps', 0, lo=0),
            'export_audio_bitrate_kbps': pick_int('export_audio_bitrate_kbps', 0, lo=0, hi=320),
            'export_two_pass': bool(tp),
            'export_max_quality': bool(mq),
            'export_target_res': str(tr or 'auto').strip() or 'auto',
            'export_target_fps': str(tf or 'auto').strip() or 'auto',
        }

    @staticmethod
    def _export_container_choice_val(params: Optional[dict], cfg) -> str:
        p = params if isinstance(params, dict) else {}
        v = p.get('export_container_choice')
        if not v:
            v = getattr(cfg, 'export_container_choice', 'match_source')
        return str(v).strip() or 'match_source'

    def estimate_export_size(self, params: dict) -> dict:
        """Ballpark encoded size (+ container) for current export controls (first queued item)."""
        from export_runner import load_any_session
        from export_codec import (
            normalized_container_extension,
            resolve_export_container_extension,
            estimate_segment_lengths_s,
            estimate_effective_avg_video_kbps,
            estimate_output_size_bytes,
        )

        p = dict(params) if isinstance(params, dict) else {}
        items = p.get('items') or []
        if not isinstance(items, list) or not items:
            return {'ok': False, 'error': 'no items'}

        overlay_only = bool(p.get('overlay_only', False))
        if overlay_only:
            return {
                'ok': True,
                'skipped': True,
                'reason': 'overlay_prores',
                'hint': 'ProRes 4444 overlay size is dominated by uncompressed RGBA throughput; '
                        'see NLE disk space tips instead.',
            }

        it0 = items[0]
        csv_path = it0.get('csv_path') or it0.get('csv')
        videos = it0.get('video_paths') or it0.get('videos') or []
        if not csv_path:
            return {'ok': False, 'error': 'no csv_path'}
        video_path = videos[0] if videos else None

        eo = self._export_encode_options(p)

        cfg = self._config
        encoder = self._resolved_export_encoder(p)
        if 'crf' in p:
            try:
                crf = max(0, min(51, int(p['crf'])))
            except (TypeError, ValueError):
                crf = int(getattr(cfg, 'crf', 18) or 18)
        else:
            crf = int(getattr(cfg, 'crf', 18) or 18)

        w = 1280
        h = 720
        fps = 30.0
        vd = 0.0
        probe: dict = {}
        if video_path:
            probe = self.get_video_probe(video_path)
            if isinstance(probe, dict) and probe.get('ok'):
                w = int(probe.get('width') or w)
                h = int(probe.get('height') or h)
                fps = float(probe.get('fps') or fps)
                vd = float(probe.get('duration') or 0.0)

        try:
            sess = load_any_session(csv_path)
        except Exception as e:
            return {'ok': False, 'error': f'load session: {e}'}

        tw, th, tfps = w, h, fps
        trs = str(eo.get('export_target_res') or 'auto').strip().lower()
        if trs != 'auto' and 'x' in trs:
            try:
                a, b = trs.split('x', 1)
                tw = max(2, int(float(a)) - int(float(a)) % 2)
                th = max(2, int(float(b)) - int(float(b)) % 2)
            except Exception:
                tw, th = w, h
        tfs = str(eo.get('export_target_fps') or 'auto').strip().lower()
        if tfs != 'auto':
            try:
                tfps = float(tfs)
            except Exception:
                tfps = fps

        scope = str(p.get('scope') or 'full')
        try:
            padding = float(p.get('padding', 5.0) or 0.0)
        except (TypeError, ValueError):
            padding = 5.0
        try:
            clip_start_s = float(p.get('clip_start_s', 0.0) or 0.0)
        except (TypeError, ValueError):
            clip_start_s = 0.0
        try:
            clip_end_s = float(p.get('clip_end_s', 0.0) or 0.0)
        except (TypeError, ValueError):
            clip_end_s = 0.0

        segs = estimate_segment_lengths_s(
            sess,
            item=dict(it0) if isinstance(it0, dict) else {},
            scope=scope,
            padding=padding,
            clip_start_s=clip_start_s,
            clip_end_s=clip_end_s,
            video_duration_s=vd,
        )

        vavg = estimate_effective_avg_video_kbps(
            encoder,
            eo['export_rate_mode'],
            crf,
            eo['export_video_bitrate_kbps'],
            eo['export_video_max_bitrate_kbps'],
            tw,
            th,
            tfps,
        )
        ab_cfg = int(eo['export_audio_bitrate_kbps'])
        ab = ab_cfg
        if ab <= 0:
            try:
                ab = int(probe.get('audio_bitrate_kbps') or 0)
            except (TypeError, ValueError):
                ab = 0
        if ab <= 0:
            ab = 128
        tp = eo['export_two_pass']

        total_bytes = 0
        for sec in segs:
            total_bytes += estimate_output_size_bytes(float(sec), vavg, float(ab), bool(tp))

        cc = WebviewAPI._export_container_choice_val(p, cfg)
        ext_eff = resolve_export_container_extension(video_path, overlay_only, cc)
        total_seconds = float(sum(float(s) for s in segs))

        hint = ''
        if len(items) > 1:
            hint = ('估算仅依据队列中第一项；导出多个节次 / 会话时体积为各自输出之和。')

        return {
            'ok': True,
            'skipped': False,
            'hint': hint,
            'encoder': encoder,
            'rate_mode': eo['export_rate_mode'],
            'width': int(w),
            'height': int(h),
            'fps': fps,
            'target_width': int(tw),
            'target_height': int(th),
            'target_fps': float(tfps),
            'video_duration_s': vd,
            'segment_lengths_s': segs,
            'segment_count': len(segs),
            'estimated_bytes': int(total_bytes),
            'total_bytes': int(total_bytes),
            'estimated_mb': round(total_bytes / (1024 * 1024), 2),
            'total_seconds': total_seconds,
            'effective_video_kbps': round(float(vavg), 1),
            'audio_bitrate_kbps': int(ab),
            'container_extension': ext_eff,
            'output_extension': ext_eff,
            'source_extension': normalized_container_extension(video_path),
            'cq': crf,
        }

    # ── Overlay ───────────────────────────────────────────────────────────────
    def get_overlay(self) -> dict:
        return asdict(self._config.overlay)

    def save_overlay(self, data: dict) -> None:
        self._config.overlay = overlay_from_dict(data)
        self._config.save()

    def save_overlay_as(self, name: str, data: dict) -> None:
        self._config.presets[name] = data
        self._config.overlay = overlay_from_dict(data)
        self._config.active_preset = name
        self._config.save()

    def list_presets(self) -> list:
        return list(self._config.presets.keys())

    # ── Session scanning ──────────────────────────────────────────────────────
    def scan_sessions(self, folder: str) -> list:
        """
        Scan a folder for telemetry files and match them to videos.
        Pass folder='__cache__' to return the last cached scan result.
        Returns a list of session dicts consumable by the JS Data page.
        """
        if folder == '__cache__':
            return self._cached_sessions()

        from session_scanner import (
            scan_csvs, scan_videos, group_videos, match_sessions,
            scan_pending_xrk, convert_xrk_files, MatchedSession,
        )

        folder = str(Path(folder).resolve())
        video_folder = self._config.video_path or folder

        # Auto-convert any XRK files that don't yet have a CSV.
        # Progress messages are pushed to JS so the status bar stays informative.
        pending_xrk = scan_pending_xrk(folder)
        if pending_xrk:
            def _xrk_progress(msg: str) -> None:
                self._push('scan_status', message=msg)
            convert_xrk_files(folder, progress_cb=_xrk_progress)

        # Scan telemetry files (includes any CSVs just produced above)
        csv_paths = scan_csvs(folder)

        # Scan video files
        try:
            videos = scan_videos(video_folder)
        except Exception:
            videos = []

        groups = group_videos(videos)
        matches = match_sessions(
            csv_paths,
            groups,
        )

        # Any XRK that still has no CSV (DLL missing / conversion failed) →
        # show as a pending session so the user can retry manually.
        existing_csv_paths = {m.csv_path for m in matches}
        for xrk_path, csv_path in scan_pending_xrk(folder):
            if csv_path not in existing_csv_paths:
                matches.append(MatchedSession(
                    csv_path        = csv_path,
                    video_group     = None,
                    time_delta      = float('inf'),
                    csv_start       = None,
                    video_start     = None,
                    matched         = False,
                    source          = 'AIM',
                    needs_conversion= True,
                    xrk_path        = xrk_path,
                ))

        # Load cached offsets
        offsets        = self._config.offsets
        offset_sources = self._config.offset_sources
        auto_failed    = set(self._config.auto_sync_failed)

        def _lookup_sync(csv_path: str):
            abs_csv = str(Path(csv_path).resolve())
            candidates = [csv_path, abs_csv]
            # Backward-compat: Windows path separator variants
            if '\\' in csv_path:
                candidates.append(csv_path.replace('\\', '/'))
            if '/' in csv_path:
                candidates.append(csv_path.replace('/', '\\'))
            if '\\' in abs_csv:
                candidates.append(abs_csv.replace('\\', '/'))
            if '/' in abs_csv:
                candidates.append(abs_csv.replace('/', '\\'))
            for k in candidates:
                if k in offsets:
                    return offsets.get(k), offset_sources.get(k)
            return None, None

        result = []
        for m in matches:
            csv = m.csv_path
            abs_csv = str(Path(csv).resolve())
            sync_offset, sync_source = _lookup_sync(csv)
            si = self._config.session_info.get(abs_csv, {}) if isinstance(self._config.session_info, dict) else {}
            video_override = si.get('_video_override')
            if video_override and os.path.isfile(video_override):
                video_paths = [video_override]
                matched = True
            else:
                video_paths = m.video_group.paths if m.video_group else []
                matched = m.matched
            track = ''
            laps_str = ''
            best_str = None
            try:
                sess = self._load_session(csv)
                if sess:
                    laps = list(getattr(sess, 'laps', []) or [])
                    timed = [l for l in laps if not getattr(l, 'is_outlap', False) and not getattr(l, 'is_inlap', False)]
                    durs = [float(l.duration) for l in timed if getattr(l, 'duration', None) is not None]
                    if not durs:
                        durs = [float(l.duration) for l in laps if getattr(l, 'duration', None) is not None]
                    best = min(durs) if durs else None
                    track = (getattr(sess, 'track', '') or '').strip()
                    laps_str = str(len(laps)) if laps else '1'
                    best_str = f'{best:.3f}s' if best is not None else None
            except Exception:
                logger.debug('scan_sessions: quick meta failed for %s', csv, exc_info=True)
            result.append({
                'csv_path':         csv,
                'source':           m.source,
                'csv_start':        m.csv_start.isoformat() if m.csv_start else None,
                'matched':          matched,
                'needs_conversion': m.needs_conversion,
                'xrk_path':        m.xrk_path,
                'video_paths':     video_paths,
                'sync_offset':     sync_offset,
                'sync_source':     sync_source,
                'auto_sync_failed': csv in auto_failed,
                'track':           track,
                'laps':            laps_str,
                'best':            best_str,
            })

        logger.info('scan_sessions: %s → %d sessions', folder, len(result))
        return result

    def save_sessions_cache(self, sessions: list) -> None:
        """Persist the full merged session list (from all paths) for fast startup.

        Called by JS after collecting results from all telemetry paths so the
        cache always reflects the complete set, not just the last path scanned.
        """
        import json
        from pathlib import Path as _Path
        from app_config import SCAN_CACHE_FILE
        try:
            SCAN_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            data = {'sessions': sessions}
            with open(SCAN_CACHE_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
            logger.info('Saved %d sessions to scan cache', len(sessions))
        except Exception:
            logger.exception('Failed to save sessions cache')

    def _cached_sessions(self) -> list:
        """Return cached sessions from disk without rescanning."""
        cache          = load_scan_cache()
        sessions       = cache.get('sessions', [])
        offsets        = self._config.offsets
        offset_sources = self._config.offset_sources
        auto_failed    = set(self._config.auto_sync_failed)

        def _lookup_sync(csv_path: str):
            abs_csv = str(Path(csv_path).resolve()) if csv_path else ''
            candidates = [csv_path, abs_csv]
            if '\\' in csv_path:
                candidates.append(csv_path.replace('\\', '/'))
            if '/' in csv_path:
                candidates.append(csv_path.replace('/', '\\'))
            if '\\' in abs_csv:
                candidates.append(abs_csv.replace('\\', '/'))
            if '/' in abs_csv:
                candidates.append(abs_csv.replace('/', '\\'))
            for k in candidates:
                if k in offsets:
                    return offsets.get(k), offset_sources.get(k)
            return None, None

        result = []
        for s in sessions:
            csv = s.get('csv_path', '')
            abs_csv = str(Path(csv).resolve()) if csv else ''
            sync_offset, sync_source = _lookup_sync(csv)
            si = self._config.session_info.get(abs_csv, {}) if isinstance(self._config.session_info, dict) else {}
            video_override = si.get('_video_override')
            cached_paths = s.get('video_paths', [])
            if video_override and os.path.isfile(video_override):
                video_paths = [video_override]
                matched = True
            else:
                video_paths = cached_paths
                matched = s.get('matched', False)
            result.append({
                'csv_path':         csv,
                'source':           s.get('source', 'RaceBox'),
                'csv_start':        s.get('csv_start'),
                'matched':          matched,
                'needs_conversion': s.get('needs_conversion', False),
                'xrk_path':        s.get('xrk_path'),
                'video_paths':     video_paths,
                'sync_offset':     sync_offset,
                'sync_source':     sync_source,
                'auto_sync_failed': csv in auto_failed,
                'track':           s.get('track', ''),
                'laps':            s.get('laps', ''),
                'best':            s.get('best') or None,
            })
        return result

    # ── Session metadata (fast header read) ──────────────────────────────────
    def get_session_meta(self, csv_path: str) -> dict:
        """
        Quick read of track name, lap count, and best lap time.
        Reads only the CSV header block — does not parse all data points.
        """
        try:
            from telemetry_algorithms import build_effective_session_meta
            session = self._load_session(csv_path)
            if session:
                from weather import fetch_weather
                overrides = self._config.session_info.get(os.path.abspath(csv_path), {}) or {}
                return build_effective_session_meta(
                    session,
                    info_overrides=overrides,
                    weather_fetcher=fetch_weather,
                )

            suffix = os.path.splitext(csv_path)[1].lower()

            # GPX / MoTeC / VBOX: need a full load but they're usually small
            if suffix in ('.gpx', '.ld', '.vbo'):
                session = self._load_session(csv_path)
                if not session:
                    return {'track': '', 'laps': '', 'best': '', 'best_secs': None}
                laps = getattr(session, 'laps', [])
                durs = [l.duration for l in laps if l.duration and not l.is_outlap and not l.is_inlap]
                best = min(durs) if durs else None
                out = {
                    'track':     getattr(session, 'track', '') or '',
                    'laps':      str(len(laps)),
                    'best':      f'{best:.3f}s' if best else '',
                    'best_secs': best,
                    'info_track':   getattr(session, 'track', '') or '',
                    'info_vehicle': getattr(session, 'vehicle', '') or '',
                    'info_session': getattr(session, 'session_type', '') or '',
                    'info_date': '',
                    'info_time': '',
                    'info_weather': '',
                    'info_wind': '',
                }
                if getattr(session, 'date_utc', None):
                    try:
                        from datetime import datetime
                        dt = datetime.fromisoformat(session.date_utc.replace('Z', '+00:00'))
                        out['info_date'] = dt.strftime('%Y-%m-%d')
                        out['info_time'] = dt.strftime('%H:%M')
                    except Exception:
                        pass
                    try:
                        first_gps = next(
                            (p for p in session.all_points
                             if getattr(p, 'lat', 0.0) and getattr(p, 'lon', 0.0)),
                            None)
                        if first_gps:
                            from weather import fetch_weather
                            out['info_weather'], out['info_wind'] = fetch_weather(
                                first_gps.lat, first_gps.lon, session.date_utc)
                    except Exception:
                        pass
                return out

            # AIM CSV: no metadata header; use filename
            if suffix == '.csv':
                track = laps_str = best_str = ''
                best_secs = None
                with open(csv_path, encoding='utf-8-sig', errors='ignore') as f:
                    first = f.readline()
                    if first.startswith('Time (s),'):
                        # AIM format — no header block
                        return {
                            'track': '',
                            'laps': '',
                            'best': '',
                            'best_secs': None,
                            'info_track': '',
                            'info_vehicle': '',
                            'info_session': '',
                            'info_date': '',
                            'info_time': '',
                            'info_weather': '',
                            'info_wind': '',
                        }
                    # RaceBox CSV — key:value header
                    from itertools import chain
                    for line in chain([first], f):
                        if line.startswith('Track,'):
                            track = line.strip().split(',', 1)[1]
                        elif line.startswith('Laps,'):
                            laps_str = line.strip().split(',', 1)[1]
                        elif line.startswith('Best Lap Time,'):
                            raw = line.strip().split(',', 1)[1]
                            try:
                                best_secs = float(raw)
                                best_str  = f'{best_secs:.3f}s'
                            except Exception:
                                best_str = raw
                        elif line.startswith('Record,'):
                            break
                return {
                    'track': track, 'laps': laps_str, 'best': best_str, 'best_secs': best_secs,
                    'info_track': track or '',
                    'info_vehicle': '',
                    'info_session': '',
                    'info_date': '',
                    'info_time': '',
                    'info_weather': '',
                    'info_wind': '',
                }

        except Exception:
            logger.exception('get_session_meta failed for %s', csv_path)
        return {
            'track': '', 'laps': '', 'best': '', 'best_secs': None,
            'info_track': '', 'info_vehicle': '', 'info_session': '',
            'info_date': '', 'info_time': '', 'info_weather': '', 'info_wind': '',
        }

    # ── Lap loading ───────────────────────────────────────────────────────────
    def get_laps(self, csv_path: str) -> list:
        """Return lap list for a session: [{lap_idx, duration, is_best}]."""
        try:
            session = self._load_session(csv_path)
            if not session or not session.laps:
                return []

            best_dur = min((l.duration for l in session.timed_laps if l.duration), default=None)
            result = []
            for i, lap in enumerate(session.laps):
                result.append({
                    'lap_idx':      i,
                    'lap_num':      lap.lap_num,
                    'duration':     lap.duration,
                    'is_best':      (not lap.is_outlap and not lap.is_inlap
                                     and lap.duration is not None and best_dur is not None
                                     and abs(lap.duration - best_dur) < 0.001),
                    'elapsed_start': round(lap.elapsed_start, 3) if hasattr(lap, 'elapsed_start') and lap.elapsed_start is not None else 0.0,
                    'is_outlap':    lap.is_outlap if hasattr(lap, 'is_outlap') else False,
                    'is_inlap':     lap.is_inlap  if hasattr(lap, 'is_inlap')  else False,
                })
            return result
        except Exception:
            logger.exception('get_laps failed for %s', csv_path)
            return []

    def resolve_preview_reference_lap(self, csv_path: str, lap_idx: int,
                                      ref_mode: str,
                                      ref_lap_csv_path: str = '',
                                      ref_lap_num: int = 0) -> dict:
        """Resolve preview reference lap in Python (same policy as export)."""
        try:
            from app_config import load_scan_cache
            from reference_resolver import resolve_reference_lap
            sess = self._load_session(csv_path)
            if not sess or lap_idx >= len(sess.laps):
                return {'ok': False, 'ref_csv_path': '', 'ref_lap_num': 0, 'desc': 'invalid session/lap'}

            cur_lap_num = getattr(sess.laps[lap_idx], 'lap_num', None)
            ref_lap, desc = resolve_reference_lap(
                ref_mode=ref_mode or 'none',
                sess=sess,
                session_info=self._config.session_info or {},
                scan_cache=load_scan_cache(),
                ref_lap_csv_path=ref_lap_csv_path or '',
                ref_lap_num=int(ref_lap_num or 0),
                current_lap_num=cur_lap_num,
                current_lap_idx=int(lap_idx),
                load_session_fn=self._load_session,
            )
            if not ref_lap:
                return {'ok': True, 'ref_csv_path': '', 'ref_lap_num': 0, 'desc': desc}
            src_csv = getattr(ref_lap, '_source_csv_path', '') or ''
            if not src_csv:
                src_csv = ref_lap_csv_path if ref_mode == 'manual' else csv_path
            return {
                'ok': True,
                'ref_csv_path': os.path.abspath(src_csv),
                'ref_lap_num': int(getattr(ref_lap, 'lap_num', 0) or 0),
                'desc': desc,
            }
        except Exception as e:
            logger.exception('resolve_preview_reference_lap failed for %s lap %d: %s', csv_path, lap_idx, e)
            return {'ok': False, 'ref_csv_path': '', 'ref_lap_num': 0, 'desc': str(e)}

    def set_lap_tag(self, csv_path: str, lap_num: int, tag: str, enabled: bool) -> dict:
        """Manually set/clear outlap or inlap tag for a lap number."""
        if tag not in ('outlap', 'inlap'):
            return {'ok': False, 'error': 'tag must be outlap or inlap'}
        abs_path = os.path.abspath(csv_path)
        store = self._config.lap_flags.get(abs_path, {'outlap': [], 'inlap': []})
        out = set(int(x) for x in store.get('outlap', []))
        inn = set(int(x) for x in store.get('inlap', []))
        target = out if tag == 'outlap' else inn
        if enabled:
            target.add(int(lap_num))
        else:
            target.discard(int(lap_num))
        self._config.lap_flags[abs_path] = {
            'outlap': sorted(out),
            'inlap': sorted(inn),
        }
        self._config.save()
        return {'ok': True}

    def load_lap_history(self, csv_path: str, lap_idx: int) -> list:
        """Return telemetry data points for one lap as a list of dicts."""
        try:
            from telemetry_algorithms import (
                apply_g_meter_smoothing_inplace,
                channel_fields_from_datapoint,
                sample_session_points,
            )
            session = self._load_session(csv_path)
            if not session or lap_idx >= len(session.laps):
                return []
            lap = session.laps[lap_idx]
            if not lap.points:
                return []
            lap_start = float(lap.points[0].elapsed)
            lap_end = lap_start + float(lap.duration or 0.0)
            samples = sample_session_points(session, lap_start, lap_end, sample_hz=60.0)
            points = []
            for _, p in samples:
                d = channel_fields_from_datapoint(p)
                d['t'] = float(p.lap_elapsed)
                points.append(d)
            apply_g_meter_smoothing_inplace(points)
            return points
        except Exception as e:
            logger.exception('load_lap_history failed for %s lap %d: %s', csv_path, lap_idx, e)
            return []

    def load_preview_history(self, csv_path: str, lap_idx: int) -> list:
        """Telemetry from the start of *lap_idx* through the end of the session.

        Used by the overlay editor preview so playback can continue past the
        selected lap's finish line (same timeline as ``vid_t - sync_offset -
        lap.elapsed_start``).

        Each dict includes:

        - ``t``: lap_elapsed for that sample's lap (same as :meth:`load_lap_history`).
        - ``sess_rel``: ``point.elapsed - lap_start_elapsed`` (monotonic along the preview).
        - ``lap``: lap number from the telemetry row.
        """
        try:
            from telemetry_algorithms import (
                apply_g_meter_smoothing_inplace,
                build_lap_info_lookup,
                build_history_row,
                lap_info_fields_for_sample,
                lap_time_display_value,
                sample_session_points,
            )
            session = self._load_session(csv_path)
            if not session or lap_idx >= len(session.laps):
                return []
            lap = session.laps[lap_idx]
            if not lap.points:
                return []
            t0 = float(lap.points[0].elapsed)
            lap_dur = float(lap.duration or 0.0)
            lap_info_lookup = build_lap_info_lookup(session.laps)
            points = []
            end_elapsed = float(session.all_points[-1].elapsed) if session.all_points else t0
            samples = sample_session_points(session, t0, end_elapsed, sample_hz=60.0)
            for sess_abs, p in samples:
                sess_rel = float(sess_abs) - t0
                li = lap_info_fields_for_sample(
                    session.laps, float(sess_abs), int(p.lap), lap_info_lookup
                )
                lap_t_display = lap_time_display_value(
                    raw_lap_t=sess_rel,
                    lap_dur=lap_dur,
                    live_lap_elapsed=float(p.lap_elapsed),
                )
                d = build_history_row(
                    p=p,
                    lap_t=lap_t_display,
                    delta_time=None,
                    lap_info=li,
                )
                d['t_display'] = float(lap_t_display)
                d['sess_rel'] = sess_rel
                d['lap'] = int(p.lap)
                points.append(d)
            apply_g_meter_smoothing_inplace(points)
            return points
        except Exception as e:
            logger.exception('load_preview_history failed for %s lap %d: %s', csv_path, lap_idx, e)
            return []

    def compute_preview_delta(self, csv_path: str, lap_idx: int,
                              ref_csv_path: str, ref_lap_num: int,
                              ref_mode: str = '') -> list:
        """Compute per-sample delta series for editor preview.

        Uses the same delta core as export (delta_time.make_delta_fn +
        distance-profile interpolation), and returns one value per sample in
        ``load_preview_history(csv_path, lap_idx)`` order.
        """
        try:
            from delta_time import compute_lap_profile

            cur_sess = self._load_session(csv_path)
            if not cur_sess or lap_idx >= len(cur_sess.laps):
                return []
            cur_lap = cur_sess.laps[lap_idx]

            dynamic_so_far = (ref_mode == 'session_best_so_far')
            ref_lap = None
            if not dynamic_so_far:
                if not ref_csv_path or not ref_lap_num:
                    return []
                ref_sess = self._load_session(ref_csv_path)
                if not ref_sess:
                    return []
                ref_lap = next((l for l in ref_sess.laps if int(getattr(l, 'lap_num', 0)) == int(ref_lap_num)), None)
                if ref_lap is None:
                    return []

            cur_t, cur_d = compute_lap_profile(cur_lap)
            if len(cur_t) < 2 or len(cur_d) < 2:
                return []

            from telemetry_algorithms import compute_preview_delta_series, sample_session_points

            t0 = float(cur_lap.points[0].elapsed) if cur_lap.points else 0.0
            end_elapsed = float(cur_sess.all_points[-1].elapsed) if cur_sess.all_points else t0
            samples = sample_session_points(cur_sess, t0, end_elapsed, sample_hz=60.0)
            return compute_preview_delta_series(cur_sess, samples, ref_lap, dynamic_so_far)
        except Exception as e:
            logger.exception('compute_preview_delta failed for %s lap %d: %s', csv_path, lap_idx, e)
            return []

    def get_preview_map_tracks(self, csv_path: str, lap_idx: int,
                               ref_csv_path: str = '', ref_lap_num: int = 0) -> dict:
        """Return preprocessed map tracks for preview (shared with export logic)."""
        try:
            from telemetry_algorithms import (
                MAP_MAX_POINTS,
                MAP_REF_SMOOTH_WINDOW,
                MAP_SMOOTH_WINDOW,
                MAP_TIMED_SAMPLES,
                build_complete_map_track,
                build_map_track,
            )
            session = self._load_session(csv_path)
            if not session or lap_idx >= len(session.laps):
                return {'lap_lats': [], 'lap_lons': [], 'ref_lats': [], 'ref_lons': []}
            lap_lats, lap_lons = build_complete_map_track(
                session.laps,
                max_points=MAP_MAX_POINTS,
                smooth_window=MAP_SMOOTH_WINDOW,
                timed_samples=MAP_TIMED_SAMPLES,
            )
            ref_lats, ref_lons = [], []
            if ref_csv_path and ref_lap_num:
                ref_sess = self._load_session(ref_csv_path)
                if ref_sess:
                    ref_lap = next(
                        (l for l in ref_sess.laps if int(getattr(l, 'lap_num', 0)) == int(ref_lap_num)),
                        None
                    )
                    if ref_lap:
                        ref_lats, ref_lons = build_map_track(
                            ref_lap.points,
                            max_points=MAP_MAX_POINTS,
                            smooth_window=MAP_REF_SMOOTH_WINDOW,
                        )
            return {
                'lap_lats': lap_lats, 'lap_lons': lap_lons,
                'ref_lats': ref_lats, 'ref_lons': ref_lons,
            }
        except Exception as e:
            logger.exception('get_preview_map_tracks failed for %s lap %d: %s', csv_path, lap_idx, e)
            return {'lap_lats': [], 'lap_lons': [], 'ref_lats': [], 'ref_lons': []}

    # ── File dialogs ──────────────────────────────────────────────────────────
    def open_folder_dialog(self) -> Optional[str]:
        if self._window is None:
            return None
        result = self._window.create_file_dialog(
            webview.FOLDER_DIALOG
        )
        if result:
            return str(Path(result[0]).resolve())
        return None

    def open_file_dialog(self, filters: list = None) -> Optional[str]:
        if self._window is None:
            return None
        result = self._window.create_file_dialog(
            webview.OPEN_DIALOG,
            file_types=filters or []
        )
        if result:
            return str(Path(result[0]).resolve())
        return None

    # ── Weather ───────────────────────────────────────────────────────────────
    def get_weather(self, lat: float, lon: float, date_iso: str) -> dict:
        try:
            from weather import fetch_weather
            weather_str, wind_str = fetch_weather(lat, lon, date_iso)
            return {'weather': weather_str, 'wind': wind_str}
        except Exception:
            return {'weather': '—', 'wind': '—'}

    def get_telemetry_tuning(self) -> dict:
        """Return backend telemetry algorithm tuning constants for UI parity."""
        try:
            from gauge_channels import G_EMA_ALPHA
            return {'g_ema_alpha': float(G_EMA_ALPHA)}
        except Exception:
            return {'g_ema_alpha': 0.10}

    def get_channel_meta(self) -> dict:
        """Return channel metadata as a frontend-parity source-of-truth."""
        try:
            from gauge_channels import GAUGE_CHANNELS
            return {k: dict(v) for k, v in (GAUGE_CHANNELS or {}).items()}
        except Exception:
            return {}

    def get_editor_catalog(self) -> dict:
        """Return editor catalog data (themes + per-channel styles)."""
        try:
            from gauge_channels import GAUGE_COLOURS, INFO_FIELDS_DEFAULT, get_channel_styles
            from overlay_themes import DEFAULT_THEME, theme_names

            channels = [
                'speed', 'rpm', 'exhaust_temp', 'gforce_lon', 'gforce_lat',
                'g_meter', 'lean', 'altitude', 'lap_time', 'delta_time',
                'map', 'info', 'lap_info', 'multi', 'image',
            ]
            labels = {
                'speed': '速度',
                'rpm': 'RPM',
                'exhaust_temp': '排气温度',
                'gforce_lon': '纵向 G',
                'gforce_lat': '横向 G',
                'g_meter': 'G 仪表',
                'lean': '倾角',
                'altitude': '海拔',
                'lap_time': '圈速',
                'delta_time': '实时秒差',
                'map': '地图',
                'info': '本节信息',
                'lap_info': '单圈信息',
                'multi': '多曲线',
                'image': '图片 / Logo',
            }
            multi_channels = ['speed', 'rpm', 'exhaust_temp', 'gforce_lon', 'gforce_lat', 'lean', 'altitude', 'lap_time', 'delta_time']
            channel_styles = {ch: list(get_channel_styles(ch)) for ch in channels}
            return {
                'theme_names': list(theme_names()),
                'default_theme': str(DEFAULT_THEME),
                'channel_styles': channel_styles,
                'channel_labels': labels,
                'multi_channels': multi_channels,
                'gauge_colours': list(GAUGE_COLOURS),
                'channel_defaults': {
                    'info': {'selected_fields': list(INFO_FIELDS_DEFAULT), 'info_overrides': {}, 'text_align': 'left'},
                    'lap_info': {
                        'selected_fields': ['lap', 'best', 'current', 'delta'],
                        'text_align': 'split',
                        'best_mode': 'so_far',
                    },
                    'multi': {'multi_channels': ['speed', 'gforce_lat']},
                    'image': {'image_path': '', 'opacity': 1.0, 'fit': 'contain'},
                    'map': {'zoom_radius_m': 150, 'show_ref': True, 'map_rotate_deg': 0, 'map_mirror_x': False, 'map_mirror_y': False},
                },
            }
        except Exception:
            return {
                'theme_names': ['Dark'],
                'default_theme': 'Dark',
                'channel_styles': {},
                'channel_labels': {},
                'multi_channels': [],
                'gauge_colours': [],
                'channel_defaults': {},
            }

    # ── Session info overrides ────────────────────────────────────────────────
    def edit_session_info(self, csv_path: str, overrides: dict) -> None:
        self._config.session_info[csv_path] = overrides
        self._config.save()

    def bulk_rename_track(self, csv_paths: list, new_name: str) -> dict:
        """Set the track override to new_name for each path in csv_paths.

        The caller (JS) is responsible for determining which paths to rename,
        since it has access to the enriched _meta that the backend does not.
        Returns {'updated': N}.
        """
        updated = 0
        for csv_path in csv_paths:
            if not csv_path:
                continue
            abs_path = os.path.abspath(csv_path)
            existing = self._config.session_info.get(abs_path, {})
            self._config.session_info[abs_path] = {**existing, 'info_track': new_name}
            updated += 1

        if updated:
            self._config.save()
        return {'updated': updated}

    def get_laps_for_ref_picker(self, csv_path: str) -> list:
        """Return timed laps from all sessions sharing the same track as csv_path.

        Groups laps by session for the manual reference lap picker UI.
        Returns [{csv_path, date, laps: [{lap_num, duration, is_best}]}].
        """
        from app_config import load_scan_cache
        session = self._load_session(csv_path)
        if not session:
            return []

        abs_path      = os.path.abspath(csv_path)
        base_track    = session.track or ''
        override      = self._config.session_info.get(abs_path, {}).get('info_track', '').strip()
        current_track = (override or base_track).strip().lower()

        cache   = load_scan_cache()
        entries = cache.get('sessions', [])
        results = []

        for entry in entries:
            ep = entry.get('csv_path', '')
            if not ep or not os.path.exists(ep):
                continue
            abs_ep    = os.path.abspath(ep)
            ov_track  = self._config.session_info.get(abs_ep, {}).get('info_track', '').strip()
            raw_track = entry.get('track', '').strip()
            try:
                sess = self._load_session(ep)
                if not sess:
                    continue
                # Fall back to actual session track when scan cache entry is stale/empty
                entry_trk = (ov_track or raw_track or sess.track or '').strip().lower()
                # When current session has a track name, filter to matching sessions only.
                # When it has no track name, show everything so the user isn't blocked.
                if current_track and entry_trk != current_track:
                    continue
                timed    = sess.timed_laps
                best_dur = min((l.duration for l in timed), default=None)
                laps     = [
                    {
                        'lap_num':  l.lap_num,
                        'duration': round(l.duration, 3),
                        'is_best':  best_dur is not None and abs(l.duration - best_dur) < 0.001,
                    }
                    for l in timed
                ]
                if laps:
                    results.append({
                        'csv_path': ep,
                        'date':     entry.get('csv_start', ''),
                        'laps':     laps,
                    })
            except Exception as e:
                logger.debug('get_laps_for_ref_picker: could not load %s: %s', ep, e)

        return results

    # ── Track map (OSM) ──────────────────────────────────────────────────────
    def get_track_map_candidates(self, csv_path: str) -> dict:
        """Return {candidates, selected_osm_id, auto_osm_id, track_key} for a session.

        Queries Overpass API (cached on disk). May be slow on first call.
        Returns {candidates: [], selected_osm_id: '', auto_osm_id: '', track_key: ''} on error.
        """
        from track_map_cache import fetch_candidates, auto_select
        empty = {'candidates': [], 'selected_osm_id': '', 'auto_osm_id': '', 'track_key': ''}
        try:
            session = self._load_session(csv_path)
            if not session:
                return empty
            pts  = session.all_points
            lats = [p.lat for p in pts if p.lat]
            lons = [p.lon for p in pts if p.lon]
            if not lats:
                return empty

            clat = sum(lats) / len(lats)
            clon = sum(lons) / len(lons)
            candidates = fetch_candidates(clat, clon)
            auto_id    = auto_select(candidates, lats, lons) or ''

            abs_csv    = os.path.abspath(csv_path)
            track_name = (self._config.session_info.get(abs_csv, {}).get('info_track')
                          or getattr(session, 'track', '') or '').lower().strip()
            selections = getattr(self._config, 'track_map_selections', {}) or {}
            selected_id = selections.get(track_name, '')

            # Slim down — strip full geometry to keep response size small
            slim = [
                {
                    'osm_id':          c['osm_id'],
                    'name':            c['name'],
                    'centroid_dist_m': round(c.get('centroid_dist_m', 0)),
                }
                for c in candidates
            ]
            return {
                'candidates':      slim,
                'selected_osm_id': selected_id,
                'auto_osm_id':     auto_id,
                'track_key':       track_name,
            }
        except Exception:
            logger.exception('get_track_map_candidates failed for %s', csv_path)
            return empty

    def set_track_map_selection(self, track_key: str, osm_id: str) -> None:
        """Save (or clear) the user-chosen OSM way for a track name."""
        if not isinstance(getattr(self._config, 'track_map_selections', None), dict):
            self._config.track_map_selections = {}
        key = track_key.lower().strip()
        if osm_id:
            self._config.track_map_selections[key] = str(osm_id)
        else:
            self._config.track_map_selections.pop(key, None)
        self._config.save()

    def get_track_map_geometry(self, csv_path: str,
                               centroid_lat: float = None,
                               centroid_lon: float = None) -> dict:
        """Return {lats, lons, areas} for the selected/auto OSM track map of a session.

        centroid_lat/lon should be supplied by the caller (already computed JS-side
        from loaded telemetry) so this method never needs to reload the session file.
        Overpass queries happen only via get_track_map_candidates (user-triggered).
        """
        from track_map_cache import load_geometry, load_areas, auto_select, _cache_path
        import json as _json
        try:
            abs_csv    = os.path.abspath(csv_path)
            track_name = (self._config.session_info.get(abs_csv, {}).get('info_track', '')
                          or self._fast_track_name(csv_path)).lower().strip()
            selections = getattr(self._config, 'track_map_selections', {}) or {}
            osm_id     = selections.get(track_name, '')

            # Auto-select from disk cache using caller-supplied centroid — no session load
            if not osm_id and centroid_lat is not None and centroid_lon is not None:
                grid_lat = round(centroid_lat, 1)
                grid_lon = round(centroid_lon, 1)
                cp = _cache_path(f'candidates_{grid_lat:.1f}_{grid_lon:.1f}')
                if cp.exists():
                    try:
                        with open(cp, 'r', encoding='utf-8') as f:
                            cached = _json.load(f)
                        osm_id = auto_select(cached, [centroid_lat], [centroid_lon]) or ''
                    except Exception:
                        pass

            areas = []
            if centroid_lat is not None and centroid_lon is not None:
                areas = load_areas(centroid_lat, centroid_lon)

            if not osm_id:
                return {'lats': [], 'lons': [], 'areas': areas}

            geometry = load_geometry(osm_id)
            if not geometry:
                return {'lats': [], 'lons': [], 'areas': areas}

            return {
                'lats':  [g['lat'] for g in geometry],
                'lons':  [g['lon'] for g in geometry],
                'areas': areas,
            }
        except Exception:
            logger.exception('get_track_map_geometry failed for %s', csv_path)
            return {'lats': [], 'lons': [], 'areas': []}

    @staticmethod
    def _fast_track_name(csv_path: str) -> str:
        """Read track name from CSV header only — no full session parse."""
        try:
            suffix = os.path.splitext(csv_path)[1].lower()
            if suffix == '.csv':
                with open(csv_path, encoding='utf-8-sig', errors='ignore') as fh:
                    for line in fh:
                        if line.startswith('Track,'):
                            return line.strip().split(',', 1)[1]
                        if line.startswith('Record,') or line.startswith('Time (s),'):
                            break
        except Exception:
            pass
        return ''

    # ── Export ────────────────────────────────────────────────────────────────
    def start_export(self, params: dict) -> None:
        # Stop any running auto-sync before beginning export
        self._auto_sync_cancel.set()
        with self._thread_lock:
            self._export_queue.append(dict(params) if isinstance(params, dict) else params)
            if self._export_thread and self._export_thread.is_alive():
                logger.info('export: queued job (queue_len=%d)', len(self._export_queue))
                return
            self._export_cancel.clear()
            self._export_thread = threading.Thread(
                target=self._export_queue_processor,
                daemon=True,
            )
            t = self._export_thread
        logger.info('export: starting queue processor')
        t.start()

    def _export_queue_processor(self) -> None:
        """Run export jobs strictly one after another; never drops a queued job."""
        try:
            while True:
                with self._thread_lock:
                    if not self._export_queue:
                        break
                    params = self._export_queue.pop(0)
                try:
                    self._run_export_bg(params)
                except Exception:
                    logger.exception('export: queue job crashed')
        finally:
            with self._thread_lock:
                self._export_thread = None
            self._kick_auto_sync_if_queued()

    def cancel_export(self) -> None:
        self._export_cancel.set()
        with self._thread_lock:
            self._export_queue.clear()

    @staticmethod
    def _csv_key_variants(csv_path: str) -> list[str]:
        """Path variants for matching config dict keys (Windows / resolved)."""
        if not csv_path:
            return []
        raw = str(csv_path)
        out: list[str] = [raw]
        try:
            out.append(str(Path(raw).resolve()))
        except Exception:
            pass
        if '\\' in raw:
            out.append(raw.replace('\\', '/'))
        if '/' in raw:
            out.append(raw.replace('/', '\\'))
        seen: set[str] = set()
        uniq: list[str] = []
        for x in out:
            if x and x not in seen:
                seen.add(x)
                uniq.append(x)
        return uniq

    def _offset_value_for_csv(self, csv_path: str):
        for k in self._csv_key_variants(csv_path):
            if k in self._config.offsets:
                return self._config.offsets.get(k)
        return None

    def _auto_sync_failed_for_csv(self, csv_path: str) -> bool:
        failed = set(self._config.auto_sync_failed or [])
        for k in self._csv_key_variants(csv_path):
            if k in failed:
                return True
        return False

    # ── Auto sync ─────────────────────────────────────────────────────────────
    def _kick_auto_sync_if_queued(self) -> None:
        """After export finishes, start the auto-sync runner if batches are waiting."""
        with self._thread_lock:
            if not self._auto_sync_batch_queue:
                return
            if self._export_thread is not None and self._export_thread.is_alive():
                return
            if self._auto_sync_thread is not None and self._auto_sync_thread.is_alive():
                return
            self._auto_sync_cancel.clear()
            self._auto_sync_thread = threading.Thread(
                target=self._auto_sync_runner,
                daemon=True,
            )
            t = self._auto_sync_thread
        logger.info('auto_sync: starting runner (%d batch(es) pending)', len(self._auto_sync_batch_queue))
        t.start()

    def _auto_sync_runner(self) -> None:
        """Drain ``_auto_sync_batch_queue`` sequentially (each batch may use parallel workers)."""
        me = threading.current_thread()
        try:
            while True:
                with self._thread_lock:
                    if not self._auto_sync_batch_queue:
                        break
                    batch = self._auto_sync_batch_queue.pop(0)
                self._auto_sync_cancel.clear()
                self._run_auto_sync_bg(batch)
        finally:
            with self._thread_lock:
                if self._auto_sync_thread is me:
                    self._auto_sync_thread = None
            self._kick_auto_sync_if_queued()

    def start_auto_sync(self, sessions: list, force: bool = False) -> dict:
        """Queue background auto-sync for sessions that need it.

        Batches are appended to a FIFO queue. If a run is already active (or
        export is running), work is still accepted and will run when the current
        work finishes.

        When ``force=True`` (manual "re-auto-sync"), computed offsets are allowed
        to overwrite existing user-locked offsets.

        Returns {'queued': N, 'reason': 'started'|'queued'|'queued_after_export'}.
        """
        if (not force) and (not self._config.auto_sync_enabled):
            logger.info('auto_sync: start skipped — auto_sync_enabled is false (use force to override)')
            return {'queued': 0, 'reason': 'disabled'}

        eligible = []
        skip_counts: dict[str, int] = {}
        skip_examples: list[tuple[str, str]] = []

        def _record_skip(reason: str, csv: str = '') -> None:
            skip_counts[reason] = skip_counts.get(reason, 0) + 1
            if csv and len(skip_examples) < 30:
                skip_examples.append((csv, reason))

        for s in sessions or []:
            csv_path = s.get('csv_path') or ''
            if not csv_path:
                _record_skip('no_csv_path')
                continue
            paths = s.get('video_paths') or []
            if not paths:
                _record_skip('no_video_paths', csv_path)
                continue
            # Scanner may leave matched=False even when user bound a video; allow
            # those sessions when force=True (manual / post-assign).
            if not s.get('matched') and not force:
                _record_skip('unmatched_session', csv_path)
                continue
            if (not force) and (self._offset_value_for_csv(csv_path) is not None):
                _record_skip('offset_already_set', csv_path)
                continue
            if (not force) and self._auto_sync_failed_for_csv(csv_path):
                _record_skip('in_auto_sync_failed_list', csv_path)
                continue
            s_run = dict(s)
            s_run['_force_overwrite_user_offset'] = bool(force)
            eligible.append(s_run)

        if not eligible:
            logger.info(
                'auto_sync: no eligible sessions force=%s requested=%d '
                'skip_counts=%s examples=%s',
                force,
                len(sessions or []),
                skip_counts,
                skip_examples,
            )
            return {'queued': 0, 'reason': 'no_eligible_sessions'}

        n = len(eligible)
        with self._thread_lock:
            self._auto_sync_batch_queue.append(eligible)
            export_busy = self._export_thread is not None and self._export_thread.is_alive()
            need_start = (not export_busy) and (
                self._auto_sync_thread is None or not self._auto_sync_thread.is_alive()
            )
            if need_start:
                self._auto_sync_cancel.clear()
                self._auto_sync_thread = threading.Thread(
                    target=self._auto_sync_runner,
                    daemon=True,
                )
                t = self._auto_sync_thread
            else:
                t = None
            pending = len(self._auto_sync_batch_queue)

        if export_busy:
            reason = 'queued_after_export'
            logger.info(
                'auto_sync: batch with %d session(s) queued after export (pending_batches=%d)',
                n,
                pending,
            )
        elif t is not None:
            reason = 'started'
            logger.info('auto_sync: starting runner with %d session(s) (pending_batches=%d)', n, pending)
            t.start()
        else:
            reason = 'queued'
            logger.info(
                'auto_sync: batch with %d session(s) appended to queue (runner already active, pending=%d)',
                n,
                pending,
            )

        return {'queued': n, 'reason': reason}

    def cancel_auto_sync(self) -> None:
        self._auto_sync_cancel.set()
        with self._thread_lock:
            self._auto_sync_batch_queue.clear()

    def _process_one_auto_sync_session(self, idx_1based: int, total: int, s: dict) -> None:
        """Run auto-sync for one session; safe to call from worker threads."""
        from auto_sync import MIN_CONFIDENCE, run_auto_sync

        if self._auto_sync_cancel.is_set():
            return
        if self._export_thread and self._export_thread.is_alive():
            return

        csv_path = s['csv_path']
        logger.info(
            'auto_sync: [%d/%d] processing %s videos=%d source=%r',
            idx_1based,
            total,
            csv_path,
            len(s.get('video_paths') or []),
            s.get('source', 'RaceBox'),
        )
        self._push(
            'auto_sync_progress',
            status='processing',
            csv_path=csv_path,
            current=idx_1based,
            total=total,
        )

        def _progress(vid_t, prog_off, prog_conf, _csv=csv_path, **extra):
            # Always send batch position so JS can render 第 n/m 节 even if a
            # prior ``processing`` push was missed or skipped (path mismatch).
            # (Do not name these ``offset``/``conf`` — on some Python versions that
            # can make ``offset`` local to the outer function and break the unpack
            # below before assignment → NameError at ``if offset is not None``.)
            payload = {
                'status': 'checking',
                'csv_path': _csv,
                'vid_t': vid_t,
                'offset': prog_off,
                'confidence': prog_conf,
            }
            if isinstance(extra, dict):
                payload.update(extra)
            payload['current'] = idx_1based
            payload['total'] = total
            self._push('auto_sync_progress', **payload)

        sync_off, sync_conf = run_auto_sync(
            csv_path     = csv_path,
            video_paths  = s.get('video_paths', []),
            source       = s.get('source', 'RaceBox'),
            cancel_event = self._auto_sync_cancel,
            progress_cb  = _progress,
            use_motion   = bool(getattr(self._config, 'auto_sync_use_motion', False)),
        )

        if self._auto_sync_cancel.is_set():
            return
        if self._export_thread and self._export_thread.is_alive():
            return

        with self._auto_sync_cfg_lock:
            if sync_off is not None:
                offset_src = (
                    'auto_baseline'
                    if sync_conf is not None and float(sync_conf) < float(MIN_CONFIDENCE)
                    else 'auto'
                )
                cur_src = None
                stored = None
                for k in self._csv_key_variants(csv_path):
                    if k in self._config.offsets and stored is None:
                        stored = self._config.offsets.get(k)
                    if k in self._config.offset_sources and cur_src is None:
                        cur_src = self._config.offset_sources.get(k)

                def _user_offset_is_placeholder() -> bool:
                    """True when 'user' is almost certainly unset (e.g. 0.000) — allow metadata baseline."""
                    if cur_src != 'user':
                        return False
                    if stored is None:
                        return True
                    try:
                        return abs(float(stored)) < 1e-6
                    except (TypeError, ValueError):
                        return True

                force_overwrite_user = bool(s.get('_force_overwrite_user_offset'))
                allow_write = force_overwrite_user or (cur_src != 'user') or (
                    offset_src == 'auto_baseline' and _user_offset_is_placeholder()
                )

                if allow_write:
                    if cur_src == 'user' and offset_src == 'auto_baseline' and _user_offset_is_placeholder():
                        logger.info(
                            'auto_sync: replacing placeholder user offset (~0) with auto_baseline for %s',
                            csv_path,
                        )
                    self._config.offsets[csv_path] = sync_off
                    self._config.offset_sources[csv_path] = offset_src
                    try:
                        failed = self._config.auto_sync_failed
                        if isinstance(failed, list) and csv_path in failed:
                            failed.remove(csv_path)
                        for k in self._csv_key_variants(csv_path):
                            if k != csv_path and isinstance(failed, list) and k in failed:
                                failed.remove(k)
                    except ValueError:
                        pass
                    self._config.save()
                    logger.info(
                        'auto_sync: [%d/%d] WRITTEN %s offset=%.3fs confidence=%.3f source=%s',
                        idx_1based,
                        total,
                        csv_path,
                        sync_off,
                        sync_conf,
                        offset_src,
                    )
                    self._push(
                        'auto_sync_progress',
                        status='done',
                        csv_path=csv_path,
                        offset=sync_off,
                        confidence=sync_conf,
                        offset_source=offset_src,
                    )
                else:
                    logger.info(
                        'auto_sync: [%d/%d] skipped write %s — user offset already set while '
                        'sync computed offset=%.3fs conf=%.3f (source_would_be=%s, force_overwrite=%s)',
                        idx_1based,
                        total,
                        csv_path,
                        sync_off,
                        sync_conf,
                        offset_src,
                        force_overwrite_user,
                    )
                    try:
                        stored_f = float(stored) if stored is not None else None
                    except (TypeError, ValueError):
                        stored_f = None
                    self._push(
                        'auto_sync_progress',
                        status='skipped',
                        csv_path=csv_path,
                        offset=sync_off,
                        confidence=sync_conf,
                        offset_source=offset_src,
                        reason='user_offset_locked',
                        stored_offset=stored_f,
                        stored_source=cur_src,
                    )
            else:
                failed = self._config.auto_sync_failed
                if isinstance(failed, list) and csv_path not in failed:
                    failed.append(csv_path)
                self._config.save()
                logger.info(
                    'auto_sync: [%d/%d] FAILED %s — low confidence or no signal '
                    '(confidence=%.3f, path added to auto_sync_failed)',
                    idx_1based,
                    total,
                    csv_path,
                    sync_conf,
                )
                self._push(
                    'auto_sync_progress',
                    status='failed',
                    csv_path=csv_path,
                    confidence=sync_conf,
                )

    def _run_auto_sync_bg(self, sessions: list) -> None:
        total = len(sessions)
        try:
            raw_workers = int(getattr(self._config, 'auto_sync_workers', 2) or 2)
        except (TypeError, ValueError):
            raw_workers = 2
        workers = max(1, min(raw_workers, 8, total, (os.cpu_count() or 4) * 2))
        logger.info(
            'auto_sync: background run started (%d session(s)), workers=%d',
            total,
            workers,
        )

        if workers <= 1:
            for i, s in enumerate(sessions):
                if self._auto_sync_cancel.is_set():
                    logger.info('auto_sync: background run stopped — cancel requested')
                    break
                if self._export_thread and self._export_thread.is_alive():
                    logger.info('auto_sync: background run stopped — export started')
                    break
                self._process_one_auto_sync_session(i + 1, total, s)
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [
                    pool.submit(self._process_one_auto_sync_session, i + 1, total, s)
                    for i, s in enumerate(sessions)
                ]
                for fut in as_completed(futures):
                    try:
                        fut.result()
                    except Exception:
                        logger.exception('auto_sync: worker task failed')

        logger.info('auto_sync: background run finished (push auto_sync_done)')
        self._push('auto_sync_done')

    def _run_export_bg(self, params: dict) -> None:
        from export_runner import run_export

        def log_cb(msg):
            self._push('export_log', message=msg)

        def progress_cb(pct, msg=''):
            self._push('export_progress', value=pct, message=msg)

        def done_cb(ok, msg=''):
            self._push('export_done', ok=ok, message=msg)

        params = dict(params) if isinstance(params, dict) else {}
        # Resolve final FFmpeg encoder (auto HW → CPU fallback) and log it once
        codec = str(params.get('export_video_codec') or getattr(self._config, 'export_video_codec', 'h264') or 'h264')
        fam   = str(params.get('export_encoder_family') or getattr(self._config, 'export_encoder_family', 'auto') or 'auto')
        resolved = self._resolved_export_encoder(params)
        params['encoder'] = resolved
        try:
            avail = self._export_encoder_probe_dict()
            # Quick human hint for auto fallback
            if fam.strip().lower() == 'auto' and resolved in ('libx264', 'libx265'):
                hw_ok = [
                    k for k, v in (avail or {}).items()
                    if v and any(t in k for t in ('_nvenc', '_amf', '_qsv', 'videotoolbox'))
                ]
                if not hw_ok:
                    log_cb(f"Resolved encoder: {resolved} (codec={codec}, family=auto) — no working HW encoder detected, fallback to CPU.")
                else:
                    log_cb(f"Resolved encoder: {resolved} (codec={codec}, family=auto) — HW encoders detected ({', '.join(hw_ok)}), but selected CPU for this codec/family.")
            else:
                log_cb(f"Resolved encoder: {resolved} (codec={codec}, family={fam})")
        except Exception:
            log_cb(f"Resolved encoder: {resolved} (codec={codec}, family={fam})")

        _workers = max(1, min(int(params.get('workers', 4)), os.cpu_count() or 4))
        _crf     = max(0, min(int(params.get('crf', 18)), 51))
        eo = self._export_encode_options(params)
        cc = WebviewAPI._export_container_choice_val(params, self._config)
        try:
            run_export(
                items             = params.get('items', []),
                scope             = params.get('scope', 'full'),
                export_path       = params.get('export_path', ''),
                encoder           = params.get('encoder', 'libx264'),
                crf               = _crf,
                workers           = _workers,
                padding           = params.get('padding', 5.0),
                is_bike           = params.get('is_bike', False),
                show_map          = params.get('show_map', True),
                show_tel          = params.get('show_tel', True),
                layout            = params.get('layout', {}),
                clip_start_s      = params.get('clip_start_s', 0.0),
                clip_end_s        = params.get('clip_end_s', 0.0),
                ref_mode          = params.get('ref_mode', 'none'),
                ref_lap_obj       = None,
                ref_lap_csv_path  = params.get('ref_lap_csv_path', ''),
                ref_lap_num       = int(params.get('ref_lap_num', 0) or 0),
                bike_overrides    = self._config.bike_overrides,
                session_info      = self._config.session_info,
                log_cb            = log_cb,
                progress_cb       = progress_cb,
                done_cb           = done_cb,
                overlay_only          = params.get('overlay_only', False),
                track_map_selections  = getattr(self._config, 'track_map_selections', {}) or {},
                lap_flags             = getattr(self._config, 'lap_flags', {}) or {},
                encode_options        = eo,
                container_choice      = cc,
            )
        except Exception as e:
            done_cb(False, str(e))

    # ── RaceBox cloud ─────────────────────────────────────────────────────────
    def racebox_playwright_status(self) -> dict:
        """Return whether playwright and Chromium are ready to use."""
        try:
            from playwright._impl._driver import compute_driver_executable
            node_exe, cli_js = compute_driver_executable()
            playwright_ok = os.path.isfile(str(node_exe))
        except Exception:
            return {'playwright': False, 'chromium': False}

        # Check if Chromium exists in PLAYWRIGHT_BROWSERS_PATH (same location
        # the runtime hook and the driver will use at runtime).
        import glob as _glob, os
        local_app = os.environ.get('LOCALAPPDATA', os.path.expanduser('~'))
        browsers_path = os.environ.get(
            'PLAYWRIGHT_BROWSERS_PATH',
            os.path.join(local_app, 'ms-playwright'),
        )
        chromium_dirs = _glob.glob(os.path.join(browsers_path, 'chromium*'))
        return {'playwright': playwright_ok, 'chromium': bool(chromium_dirs)}

    def install_playwright_chromium(self) -> None:
        """Download Chromium for Playwright in the background.
        Pushes events: racebox_setup_log {message}, racebox_setup_done {ok, message}."""
        import threading

        def _run():
            try:
                from playwright._impl._driver import compute_driver_executable
                node_exe, cli_js = compute_driver_executable()
                import subprocess
                self._push('racebox_setup_log', message='Downloading Chromium (~130 MB, one-time)…')
                env = os.environ.copy()
                proc = subprocess.Popen(
                    [str(node_exe), str(cli_js), 'install', 'chromium'],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding='utf-8', errors='replace', env=env,
                )
                # Read char-by-char so \r-terminated progress lines are captured
                buf = ''
                while True:
                    ch = proc.stdout.read(1)
                    if not ch:
                        break
                    if ch in ('\n', '\r'):
                        line = buf.strip()
                        if line:
                            self._push('racebox_setup_log', message=line)
                        buf = ''
                    else:
                        buf += ch
                if buf.strip():
                    self._push('racebox_setup_log', message=buf.strip())
                proc.wait()
                if proc.returncode == 0:
                    self._push('racebox_setup_done', ok=True,
                               message='Chromium installed. You can now use RaceBox cloud download.')
                else:
                    self._push('racebox_setup_done', ok=False,
                               message=f'Install failed (exit {proc.returncode}).')
            except Exception as e:
                self._push('racebox_setup_done', ok=False, message=f'Error: {e}')

        threading.Thread(target=_run, daemon=True).start()

    def racebox_login(self, email: str, password: str) -> dict:
        """Check whether saved RaceBox auth is still valid (headless).
        If no saved auth exists, returns a prompt to use Download Sessions instead.
        email/password args are unused — auth is browser-based via Playwright."""
        try:
            from racebox_downloader import RaceBoxSource
        except ImportError:
            return {'ok': False, 'error': 'Playwright / racebox_downloader not available in this build.'}

        src = RaceBoxSource()
        if not src.is_authenticated():
            return {
                'ok': False,
                'error': 'Not logged in yet. Click "Download Sessions" — a browser will open for first-time login.',
            }

        # Validate saved auth headlessly
        logs: list[str] = []
        ok = src.authenticate(log_cb=logs.append)
        if ok:
            return {'ok': True}
        return {'ok': False, 'error': '\n'.join(logs) or 'Auth validation failed.'}

    # ── Encoder detection ──────────────────────────────────────────────────────
    def check_encoders(self) -> dict:
        """
        Probe FFmpeg and report which video encoders are available.
        Returns {version, encoders: [{name, label, available}]} or {error}.
        """
        from utils import _run
        from ffmpeg_paths import get_ffmpeg_bin
        from export_encoder import encoder_rows_for_ui

        ffmpeg_bin = get_ffmpeg_bin()
        try:
            r = _run([ffmpeg_bin, '-version'], capture_output=True, text=True, timeout=10)
            first = (r.stdout or '').splitlines()[0] if r.stdout else ''
            version = first.split('version')[-1].strip().split(' ')[0] if 'version' in first else 'unknown'
        except Exception as e:
            return {'error': f'FFmpeg error: {e}'}

        rows = encoder_rows_for_ui(self._export_encoder_probe_dict())
        return {'version': version, 'encoders': rows}

    def resolve_export_encoder(self, codec, family=None) -> dict:
        """Resolve UI codec + encoder family to FFmpeg ``-c:v`` name (uses cached probes)."""
        from export_encoder import resolve_export_encoder as _res
        if isinstance(codec, dict):
            d = codec
            codec = d.get('codec', 'h264')
            family = d.get('family', 'auto') if family is None else family
        if family is None:
            family = 'auto'
        try:
            enc = _res(str(codec or 'h264'), str(family or 'auto'), self._export_encoder_probe_dict())
            return {'ok': True, 'encoder': enc}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    # ── About ──────────────────────────────────────────────────────────────────
    def get_about_info(self) -> dict:
        """Return diagnostic strings for the About section."""
        import sys
        from app_config import CONFIG_FILE
        from _version import __version__
        return {
            'version': __version__,
            'python': f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}',
            'config': str(CONFIG_FILE),
        }

    # ── AIM DLL status ────────────────────────────────────────────────────────
    def aim_dll_status(self) -> dict:
        """Return whether the AIM MatLabXRK DLL is present."""
        import glob as _glob, sys, os
        from pathlib import Path
        # Persistent user directory is checked first so the DLL survives app rebuilds.
        search_dirs = [str(Path.home() / '.openlap')]
        if getattr(sys, 'frozen', False):
            search_dirs += [sys._MEIPASS, os.path.dirname(sys.executable)]
        else:
            search_dirs.append(os.path.dirname(os.path.abspath(__file__)))
        for base in search_dirs:
            dlls = _glob.glob(os.path.join(base, 'MatLabXRK*.dll'))
            if dlls:
                return {'found': True, 'path': dlls[0]}
        return {'found': False, 'path': ''}

    def download_aim_dll(self) -> dict:
        """Download the AIM MatLabXRK DLL from aim-sportline.com in a background thread.
        Progress is pushed as openlap events: aim_dll_progress {value, message}, aim_dll_done {ok, message}."""
        import threading

        def _run():
            try:
                import sys, os
                from xrk_to_csv import _download_dll_urllib, _install_dll_from_zip, DLL_ZIP_URL
                self._push('aim_dll_progress', value=10, message='Connecting to aim-sportline.com…')
                data = _download_dll_urllib()
                if not data:
                    self._push('aim_dll_done', ok=False, message='Download failed — could not reach aim-sportline.com.')
                    return
                self._push('aim_dll_progress', value=70, message='Extracting DLL…')
                from pathlib import Path as _Path
                install_dir = str(_Path.home() / '.openlap')
                os.makedirs(install_dir, exist_ok=True)
                import io, zipfile, glob as _glob
                with zipfile.ZipFile(io.BytesIO(data)) as zf:
                    for entry in zf.namelist():
                        if not entry.lower().endswith('.dll'):
                            continue
                        local_name = os.path.basename(entry)
                        if not local_name:
                            continue
                        local_path = os.path.join(install_dir, local_name)
                        if os.path.isfile(local_path):
                            continue
                        with zf.open(entry) as src, open(local_path, 'wb') as dst:
                            dst.write(src.read())
                dlls = _glob.glob(os.path.join(install_dir, 'MatLabXRK*.dll'))
                if dlls:
                    self._push('aim_dll_progress', value=100, message='DLL installed.')
                    self._push('aim_dll_done', ok=True, message='MatLabXRK DLL installed — restart OpenLap to use AIM XRK conversion.')
                else:
                    self._push('aim_dll_done', ok=False, message='Zip downloaded but MatLabXRK DLL not found inside.')
            except Exception as e:
                self._push('aim_dll_done', ok=False, message=f'Error: {e}')

        threading.Thread(target=_run, daemon=True).start()

    # ── AIM XRK conversion ────────────────────────────────────────────────────
    def convert_xrk_session(self, csv_path: str) -> dict:
        """Convert a single AIM XRK file to CSV. csv_path is the expected CSV output path."""
        import os
        xrk_path = os.path.splitext(csv_path)[0]
        # Try common XRK extensions
        actual_xrk = None
        for ext in ('.xrk', '.xrz', '.drk', '.XRK', '.XRZ', '.DRK'):
            candidate = xrk_path + ext
            if os.path.isfile(candidate):
                actual_xrk = candidate
                break
        if not actual_xrk:
            return {'ok': False, 'error': 'XRK source file not found'}
        try:
            import xrk_to_csv as _xrk
            import glob as _glob, sys
            from pathlib import Path
            search_dirs = [str(Path.home() / '.openlap')]
            if getattr(sys, 'frozen', False):
                search_dirs += [sys._MEIPASS, os.path.dirname(sys.executable)]
            else:
                search_dirs.append(os.path.dirname(os.path.abspath(__file__)))
            dll_path = next(
                (d[0] for base in search_dirs
                 for d in [_glob.glob(os.path.join(base, 'MatLabXRK*.dll'))] if d),
                None
            )
            _xrk.xrk_to_csv(actual_xrk, csv_path, dll_path)
            return {'ok': True}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    # ── Manual video assignment ───────────────────────────────────────────────
    def assign_video(self, csv_path: str, video_path: str) -> None:
        """Manually link a video file to a telemetry session."""
        abs_csv = str(Path(csv_path).resolve())
        si = self._config.session_info.setdefault(abs_csv, {})
        si['_video_override'] = str(Path(video_path).resolve())
        self._config.save()

    def import_dropped_paths(self, paths: list, selected_csv_path: str = '') -> dict:
        """Handle drag-and-drop import for telemetry/video files or folders."""
        import os
        import re
        from pathlib import Path as _Path

        def _norm(p: str) -> str:
            p = (p or '').strip().strip('"').strip("'")
            if p.startswith('file:///'):
                p = p.replace('file:///', '', 1)
            p = p.replace('/', os.sep)
            # Decode simple URL-escaped spaces often found in uri-list drops
            p = p.replace('%20', ' ')
            return str(_Path(p).resolve())

        if not isinstance(paths, list) or not paths:
            return {'ok': False, 'message': 'No dropped paths received.'}

        video_ext = {'.mp4', '.mov', '.avi', '.mkv', '.m4v', '.mts', '.wmv'}
        telemetry_ext = {'.csv', '.gpx', '.ld', '.vbo', '.xrk', '.xrz', '.drk'}

        imported = []
        updated_cfg = {}
        selected_csv = _norm(selected_csv_path) if selected_csv_path else ''

        for raw in paths:
            p = _norm(str(raw))
            if not p or not os.path.exists(p):
                continue

            if os.path.isdir(p):
                # Folders: best-effort classification by quick extension scan
                has_vid = False
                has_tel = False
                for root, _, files in os.walk(p):
                    for fn in files:
                        ext = os.path.splitext(fn)[1].lower()
                        if ext in video_ext:
                            has_vid = True
                        if ext in telemetry_ext:
                            has_tel = True
                    if has_vid and has_tel:
                        break
                if has_vid:
                    updated_cfg['video_path'] = p
                    imported.append(f'video folder: {p}')
                if has_tel:
                    # Keep backward-compatible catch-all path for mixed telemetry drops
                    updated_cfg['telemetry_path'] = p
                    imported.append(f'telemetry folder: {p}')
                continue

            ext = os.path.splitext(p)[1].lower()
            parent = str(_Path(p).parent)

            if ext in video_ext:
                if selected_csv:
                    abs_csv = os.path.abspath(selected_csv)
                    si = self._config.session_info.setdefault(abs_csv, {})
                    si['_video_override'] = p
                    imported.append(f'video assigned to session: {os.path.basename(p)}')
                else:
                    updated_cfg['video_path'] = parent
                    imported.append(f'video file folder set: {parent}')
                continue

            if ext in telemetry_ext:
                if ext == '.gpx':
                    updated_cfg['gpx_path'] = parent
                elif ext == '.vbo':
                    updated_cfg['vbox_path'] = parent
                elif ext == '.ld':
                    updated_cfg['motec_path'] = parent
                elif ext in ('.xrk', '.xrz', '.drk'):
                    updated_cfg['aim_path'] = parent
                else:
                    # CSV can be RaceBox or AIM-converted; keep generic bucket.
                    updated_cfg['telemetry_path'] = parent
                imported.append(f'telemetry file folder set: {parent}')
                continue

        if updated_cfg:
            self.save_config(updated_cfg)
        else:
            # Still save if we assigned a video override above.
            self._config.save()

        if not imported:
            return {'ok': False, 'message': 'No supported telemetry/video files found in drop.'}
        return {'ok': True, 'message': '; '.join(imported), 'updated': updated_cfg}

    # ── Custom lap split tools ────────────────────────────────────────────────
    def auto_split_laps_from_json(self, json_path: str, input_dir: str, output_dir: str) -> dict:
        """Batch split .gpx/.vbo laps using start line from a track JSON file."""
        from lap_split_tools import auto_split_folder_with_track_json
        return auto_split_folder_with_track_json(json_path, input_dir, output_dir)

    def auto_split_lap_for_file(self, json_path: str, telemetry_path: str) -> dict:
        """Split laps for a selected .gpx/.vbo file in place."""
        from lap_split_tools import auto_split_file_with_track_json
        return auto_split_file_with_track_json(json_path, telemetry_path)

    def launch_manual_lap_split_gui(self, telemetry_path: str | None = None) -> dict:
        """Launch lap_split_tools/gui_split.py in a separate process.

        When ``telemetry_path`` is a .gpx/.vbo file, the GUI loads it on startup
        so the user does not need to pick the file manually.
        """
        from lap_split_tools import launch_gui_split
        return launch_gui_split(telemetry_path)

    def list_track_jsons(self) -> list:
        """List track JSON files from repository-local tracks/ directory."""
        import json
        base = Path(__file__).resolve().parent / 'tracks'
        if not base.exists():
            return []
        items = []
        for p in sorted(base.glob('*.json')):
            if p.name.endswith('.template.json'):
                continue
            display_name = p.stem
            try:
                with open(p, 'r', encoding='utf-8') as f:
                    cfg = json.load(f)
                md = cfg.get('metadata', {}) or {}
                display_name = (
                    md.get('名称')
                    or md.get('name')
                    or md.get('Name')
                    or p.stem
                )
            except Exception:
                # Fallback to filename stem when metadata parsing fails.
                display_name = p.stem
            items.append({
                'name': p.stem,
                'display_name': str(display_name),
                'path': str(p.resolve()),
            })
        return items

    # ── RaceBox session download ──────────────────────────────────────────────
    def download_racebox_sessions(self) -> None:
        """Start a background RaceBox download. Progress is pushed as events:
            racebox_log      {message}
            racebox_progress {value: 0-100, message}
            racebox_done     {ok, message, n_downloaded}
        """
        with self._thread_lock:
            if self._rb_thread and self._rb_thread.is_alive():
                return   # already running
            self._rb_cancel.clear()
            self._rb_thread = threading.Thread(
                target=self._run_racebox_bg, daemon=True)
            self._rb_thread.start()

    def cancel_racebox_download(self) -> None:
        self._rb_cancel.set()

    def _run_racebox_bg(self) -> None:
        def log(msg: str) -> None:
            self._push('racebox_log', message=msg)

        def progress(pct: float, msg: str = '') -> None:
            self._push('racebox_progress', value=pct, message=msg)

        def done(ok: bool, msg: str = '', n: int = 0) -> None:
            self._push('racebox_done', ok=ok, message=msg, n_downloaded=n)

        try:
            from racebox_downloader import RaceBoxSource
        except ImportError:
            done(False, 'Playwright / racebox_downloader not available in this build.')
            return

        dest = self._config.racebox_path or self._config.telemetry_path
        if not dest:
            done(False, 'No RaceBox folder configured — set it in Settings.')
            return

        try:
            src = RaceBoxSource(data_dir=dest)

            # Authenticate (opens browser on first run; headless thereafter)
            log('Authenticating…')
            ok = src.authenticate(log_cb=log)
            if not ok:
                done(False, 'Authentication failed.')
                return
            if self._rb_cancel.is_set():
                done(False, 'Cancelled.')
                return

            # List sessions
            log('Fetching session list from racebox.pro…')
            sessions = src.list_sessions(log_cb=log)
            if not sessions:
                done(True, 'No sessions found on racebox.pro.', 0)
                return

            new = [s for s in sessions if not src.already_downloaded(s, dest)]
            log(f'{len(sessions)} session(s) on server — {len(new)} new to download.')

            if not new:
                done(True, 'Already up to date.', 0)
                return

            # Download new sessions
            downloaded = 0
            for i, sess in enumerate(new):
                if self._rb_cancel.is_set():
                    done(False, f'Cancelled after {downloaded} download(s).',
                         downloaded)
                    return

                progress((i / len(new)) * 100, f'{i+1}/{len(new)}: {sess.label()}')
                path = src.download(sess, dest,
                                    progress_cb=None, log_cb=log)
                if path:
                    downloaded += 1

            progress(100, 'Done.')
            done(True, f'{downloaded} of {len(new)} session(s) downloaded.', downloaded)

        except Exception as exc:
            logger.exception('RaceBox download error')
            done(False, str(exc))

    # ── Internal helpers ──────────────────────────────────────────────────────
    def _load_session(self, csv_path: str):
        import gpx_data, aim_data, racebox_data, motec_data, vbox_data
        if vbox_data.is_vbox(csv_path):
            session = vbox_data.load_vbo(csv_path)
            self._apply_lap_flags(session, csv_path)
            return session
        if motec_data.is_motec_ld(csv_path):
            session = motec_data.load_ld(csv_path)
            self._apply_lap_flags(session, csv_path)
            return session
        if gpx_data.is_gpx(csv_path):
            session = gpx_data.load_gpx(csv_path)
            self._apply_lap_flags(session, csv_path)
            return session
        if aim_data.is_aim_csv(csv_path):
            session = aim_data.load_csv(csv_path)
            self._apply_lap_flags(session, csv_path)
            return session
        session = racebox_data.load_csv(csv_path)
        self._apply_lap_flags(session, csv_path)
        return session

    def _apply_lap_flags(self, session, csv_path: str) -> None:
        """Apply default and manual outlap/inlap tags to a loaded session."""
        if not session or not getattr(session, 'laps', None):
            return
        suffix = os.path.splitext(csv_path)[1].lower()

        # Default for GPX/VBO: first lap outlap, last lap inlap.
        # Keep at least one timed lap (2-lap files would otherwise become all filtered).
        if suffix in ('.gpx', '.vbo') and len(session.laps) >= 3:
            for lap in session.laps:
                lap.is_outlap = False
                lap.is_inlap = False
            session.laps[0].is_outlap = True
            session.laps[-1].is_inlap = True

        # Manual override from config.
        abs_path = os.path.abspath(csv_path)
        flags = self._config.lap_flags.get(abs_path, {}) if isinstance(self._config.lap_flags, dict) else {}
        def _to_int_set(vals):
            out = set()
            for v in vals or []:
                try:
                    out.add(int(v))
                except Exception:
                    pass
            return out
        outlaps = _to_int_set(flags.get('outlap', []))
        inlaps = _to_int_set(flags.get('inlap', []))
        if outlaps or inlaps:
            for lap in session.laps:
                if lap.lap_num in outlaps:
                    lap.is_outlap = True
                if lap.lap_num in inlaps:
                    lap.is_inlap = True

        # Keep best_lap_time consistent with filtered timed laps.
        timed_durs = [l.duration for l in session.timed_laps if l.duration]
        session.best_lap_time = min(timed_durs) if timed_durs else 0.0
