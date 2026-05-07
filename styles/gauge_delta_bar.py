"""
Gauge style: Delta Bar
======================
Centered delta bar similar to iRacing / RaceChrono.

Negative (faster) fills left in green, positive (slower) fills right in red.
"""
STYLE_NAME = 'Delta Bar'
ELEMENT_TYPE = 'gauge'

import matplotlib
matplotlib.use('Agg')
import math
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, Rectangle


def _delta_colour(delta: float) -> str:
    if abs(delta) <= 0.10:
        return '#e8e8e8'
    return '#22dd66' if delta < 0 else '#ff4444'


def _bar_magnitude(delta: float, full_scale: float = 1.0, exponent: float = 0.45) -> float:
    """Map |delta| seconds → [0..1] for half-bar length (matches frontend delta_bar.js)."""
    fs = max(0.05, float(full_scale))
    exp = max(0.15, min(1.0, float(exponent)))
    u = min(1.0, abs(float(delta)) / fs)
    return math.pow(u, exp)


def render(data: dict, w: int, h: int):
    from overlay_utils import fig_to_rgba, scale_factor, px_to_pt, font_px_to_pt

    raw = data.get('value', None)
    has_value = raw is not None and math.isfinite(float(raw))
    value = float(raw) if has_value else 0.0
    label = data.get('label', 'Delta')
    full_scale = float(data.get('delta_bar_full_scale', 1.0))
    curve_exp = float(data.get('delta_bar_curve', 0.45))

    T = data.get('_tc', {})
    bg_rgba = T.get('bg_rgba', (0, 0, 0, 0.72))
    bg_edge = T.get('bg_edge_rgba', (1, 1, 1, 0.07))
    label_col = T.get('label', '#445566')
    track_col = T.get('track', '#1a2530')

    sc = scale_factor(w, h, base_w=180, base_h=120)
    dpi = 100
    fig = plt.figure(figsize=(w / dpi, h / dpi), dpi=dpi)
    fig.patch.set_alpha(0)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_facecolor((0, 0, 0, 0))
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis('off')

    ax.add_patch(FancyBboxPatch((0.02, 0.02), 0.96, 0.96,
                                boxstyle='round,pad=0.02',
                                facecolor=bg_rgba, edgecolor=bg_edge, linewidth=1))

    fs_label = max(5, min(int(10 * sc), int(w * 0.08)))
    fs_val = max(6, min(int(15 * sc), int(w * 0.12)))
    ax.text(0.5, 0.84, label.upper(), ha='center', va='center',
            color=label_col, fontsize=font_px_to_pt(fs_label, dpi), fontfamily='sans-serif')

    x0, x1 = 0.08, 0.92
    y0, bh = 0.42, 0.20
    cx = (x0 + x1) * 0.5
    bw = x1 - x0
    ax.add_patch(Rectangle((x0, y0), bw, bh, facecolor=track_col, edgecolor='none'))

    mag = _bar_magnitude(value, full_scale, curve_exp) if has_value else 0.0
    sign = 1.0 if value > 0 else (-1.0 if value < 0 else 0.0)
    frac = sign * mag
    if frac > 0:
        ax.add_patch(Rectangle((cx, y0), (bw * 0.5) * frac, bh,
                               facecolor='#ff4444', edgecolor='none', alpha=0.9))
    elif frac < 0:
        ax.add_patch(Rectangle((cx + (bw * 0.5) * frac, y0), (bw * 0.5) * (-frac), bh,
                               facecolor='#22dd66', edgecolor='none', alpha=0.9))

    ax.plot([cx, cx], [y0 - 0.02, y0 + bh + 0.02], color='#3a4a5a',
            linewidth=px_to_pt(1.0, dpi))

    if has_value:
        txt = f"+{value:.3f}" if value >= 0 else f"\u2212{abs(value):.3f}"
        tcol = _delta_colour(float(value))
    else:
        txt = '\u2014'
        tcol = label_col
    ax.text(0.5, 0.25, txt, ha='center', va='center',
            color=tcol, fontsize=font_px_to_pt(fs_val, dpi), fontweight='bold',
            fontfamily='sans-serif')

    return fig_to_rgba(fig, (w, h))
