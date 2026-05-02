#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
对比「旧式两段」与「BGR pipe 一段」在本机 FFmpeg 上的吞吐（仅 FFmpeg，不含 OpenCV 叠字）。

旧式：lavfi 生成画面 → MJPEG 写入磁盘 → 再读盘 + scale/yuv420p + 目标编码器（模拟 *_raw.avi + mux）。
新式：同分辨率随机 BGR 帧 stdin → 同一套 -vf + 编码器（与 OpenLap 导出管道路径一致）。

说明：
  * Shutter 等工具里的 ``-hwaccel auto`` 作用于 **FFmpeg 解码输入视频**；OpenLap 叠字阶段用
    OpenCV 读源片，pipe 里只有已合成的 BGR，故不能 1:1 照搬其整条命令。
  * ``-threads 0``、MP4 下 HEVC 的 ``hvc1`` 等已在 ``video_renderer`` 的导出命令里对齐通用做法。

用法::

  python tools/benchmark_export_pipeline.py
  python tools/benchmark_export_pipeline.py --frames 600 --width 1920 --height 1080 --fps 59.94
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _ffmpeg() -> list[str]:
    sys.path.insert(0, _repo_root())
    from ffmpeg_paths import resolve_media_cmd

    return resolve_media_cmd(['ffmpeg'])


def _popen_kw() -> dict:
    sys.path.insert(0, _repo_root())
    from utils import _win_flags

    return {**_win_flags(), 'stdin': subprocess.PIPE, 'stdout': subprocess.DEVNULL, 'stderr': subprocess.PIPE}


def _enc_flags(encoder: str) -> list[str]:
    e = encoder.lower()
    if 'nvenc' in e:
        return ['-preset', 'p4', '-rc', 'vbr', '-cq', '23', '-b:v', '0']
    if e == 'libx265':
        return ['-preset', 'fast', '-crf', '28']
    if e == 'libx264':
        return ['-preset', 'veryfast', '-crf', '23']
    return ['-crf', '23']


def _probe_encoder(ffmpeg: list[str], enc: str) -> bool:
    cmd = ffmpeg + [
        '-hide_banner',
        '-loglevel',
        'error',
        '-f',
        'lavfi',
        '-i',
        'nullsrc=s=128x128:d=0.05',
        '-pix_fmt',
        'yuv420p',
        '-frames:v',
        '2',
        '-c:v',
        enc,
        '-f',
        'null',
        '-',
    ]
    r = subprocess.run(cmd, capture_output=True)
    return r.returncode == 0


def bench_legacy_mjpeg_then_encode(
    ffmpeg: list[str], n: int, w: int, h: int, fps: float, encoder: str, vf: str
) -> tuple[float, float, float]:
    """Returns (mjpeg_pass_s, encode_pass_s, mjpeg_file_mb)."""
    td = tempfile.mkdtemp(prefix='openlap_bench_')
    avi = os.path.join(td, 'raw.avi')
    t0 = time.perf_counter()
    cmd1 = ffmpeg + [
        '-y',
        '-hide_banner',
        '-loglevel',
        'error',
        '-threads',
        '0',
        '-f',
        'lavfi',
        '-i',
        f'testsrc=size={w}x{h}:rate={fps}',
        '-frames:v',
        str(n),
        '-c:v',
        'mjpeg',
        '-q:v',
        '3',
        avi,
    ]
    subprocess.run(cmd1, capture_output=True, check=True)
    t1 = time.perf_counter()
    sz_mb = os.path.getsize(avi) / (1024 * 1024)
    cmd2 = ffmpeg + [
        '-y',
        '-hide_banner',
        '-loglevel',
        'error',
        '-threads',
        '0',
        '-i',
        avi,
        '-an',
        '-vf',
        vf,
        '-c:v',
        encoder,
        *_enc_flags(encoder),
        '-f',
        'null',
        '-',
    ]
    subprocess.run(cmd2, capture_output=True, check=True)
    t2 = time.perf_counter()
    try:
        os.remove(avi)
        os.rmdir(td)
    except OSError:
        pass
    return t1 - t0, t2 - t1, sz_mb


def bench_pipe_bgr(ffmpeg: list[str], n: int, w: int, h: int, fps: float, encoder: str, vf: str) -> float:
    fps_s = f'{fps:.6f}'.rstrip('0').rstrip('.') if fps else '30'
    cmd = ffmpeg + [
        '-y',
        '-hide_banner',
        '-loglevel',
        'error',
        '-threads',
        '0',
        '-f',
        'rawvideo',
        '-pix_fmt',
        'bgr24',
        '-s',
        f'{w}x{h}',
        '-r',
        fps_s,
        '-thread_queue_size',
        '512',
        '-i',
        'pipe:0',
        '-an',
        '-vf',
        vf,
        '-c:v',
        encoder,
        *_enc_flags(encoder),
        '-f',
        'null',
        '-',
    ]
    frame = os.urandom(w * h * 3)
    t0 = time.perf_counter()
    p = subprocess.Popen(cmd, **_popen_kw())
    assert p.stdin is not None
    for _ in range(n):
        p.stdin.write(frame)
    p.stdin.close()
    err = p.stderr.read() if p.stderr else b''
    p.wait()
    t1 = time.perf_counter()
    if p.returncode != 0:
        sys.stderr.buffer.write(err)
        raise SystemExit(p.returncode)
    return t1 - t0


def main() -> None:
    ap = argparse.ArgumentParser(description='Benchmark MJPEG two-pass vs BGR pipe (FFmpeg only).')
    ap.add_argument('--frames', type=int, default=300, help='Frames per run (default 300)')
    ap.add_argument('--width', type=int, default=1280)
    ap.add_argument('--height', type=int, default=720)
    ap.add_argument('--fps', type=float, default=30.0)
    ap.add_argument('--encoders', nargs='*', default=None, help='Default: libx264 + any HW encoder that probes OK')
    args = ap.parse_args()

    ffmpeg = _ffmpeg()
    vf = 'scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p'
    want = args.encoders or ['libx264', 'hevc_nvenc', 'h264_nvenc', 'libx265']
    encoders = [e for e in want if _probe_encoder(ffmpeg, e)]
    if not encoders:
        print('No encoders probed successfully.', file=sys.stderr)
        sys.exit(1)

    n, w, h, fps = args.frames, args.width, args.height, args.fps
    print('OpenLap export pipeline benchmark (FFmpeg segment only)')
    print(f'  ffmpeg: {ffmpeg[0]}')
    print(f'  {w}x{h} @ {fps} fps  ×  {n} frames  |  vf={vf!r}')
    print()

    for enc in encoders:
        mj_s, enc_s, mj_mb = bench_legacy_mjpeg_then_encode(ffmpeg, n, w, h, fps, enc, vf)
        pipe_s = bench_pipe_bgr(ffmpeg, n, w, h, fps, enc, vf)
        legacy_total = mj_s + enc_s
        ratio = legacy_total / pipe_s if pipe_s > 0 else 0.0
        fps_legacy = n / legacy_total if legacy_total > 0 else 0.0
        fps_pipe = n / pipe_s if pipe_s > 0 else 0.0
        print(f'Encoder {enc!r}')
        print(f'  Legacy MJPEG disk:  write {mj_s:.2f}s  +  re-encode {enc_s:.2f}s  = {legacy_total:.2f}s  (~{fps_legacy:.1f} fps)  [mjpeg {mj_mb:.1f} MB]')
        print(f'  BGR pipe:           {pipe_s:.2f}s  (~{fps_pipe:.1f} fps)')
        print(f'  Ratio (legacy / pipe): {ratio:.2f}x')
        print()


if __name__ == '__main__':
    main()
