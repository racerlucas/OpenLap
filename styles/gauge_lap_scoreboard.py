"""
Gauge style: Scoreboard
=======================
Four-row numeric panel showing lap position, best completed lap,
current lap time, and live delta against that best.

ELEMENT_TYPE : "gauge"
STYLE_NAME   : "Scoreboard"
Data keys    : lap_num, total_laps, lap_elapsed, best_so_far (float | None)
"""
STYLE_NAME   = 'Scoreboard'
ELEMENT_TYPE = 'gauge'

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch


def _fmt_time(secs: float) -> str:
    """Format a lap time as M:SS.mmm."""
    m = int(secs // 60)
    s = secs % 60
    return f"{m}:{s:06.3f}"


def render(data: dict, w: int, h: int):
    from overlay_utils import fig_to_rgba

    T         = data.get('_tc', {})
    bg_rgba   = T.get('bg_rgba',      (0.04, 0.06, 0.10, 0.78))
    bg_edge   = T.get('bg_edge_rgba', (1.00, 1.00, 1.00, 0.08))
    text_col  = T.get('text',         '#f0f4f8')
    label_col = T.get('label',        '#4e6578')
    fill_pos  = T.get('fill_pos',     '#ff9f00')   # accent bar
    ok_col    = T.get('fill_lo',      '#00d4ff')   # faster (green-ish)
    err_col   = T.get('fill_hi',      '#ff4422')   # slower

    lap_num    = int(data.get('lap_num',    1))
    total_laps = int(data.get('total_laps', 1))
    elapsed    = float(data.get('lap_elapsed', 0.0))
    best_mode  = str(data.get('best_mode', 'so_far')).lower()
    if best_mode not in ('so_far', 'session'):
        best_mode = 'so_far'
    best       = data.get('session_best') if best_mode == 'session' else data.get('best_so_far')

    # ── Delta — use live reference-lap delta when available ───────────────────
    delta_raw = data.get('delta_time')
    if delta_raw is not None:
        delta      = float(delta_raw)
        delta_txt  = f"{delta:+.3f}"
        delta_col  = ok_col if delta < 0.0 else err_col
    else:
        delta_txt = "—"
        delta_col = label_col

    # ── Rows: (label, value_text, value_colour) ───────────────────────────────
    is_outlap = (lap_num == 0)
    lap_label = "OUT LAP" if is_outlap else "LAP"
    lap_val   = "—" if is_outlap else f"{lap_num} / {total_laps}"

    all_rows = {
        'lap':     (lap_label, lap_val,                        text_col),
        'best':    ("BEST",    _fmt_time(best) if best else "—", label_col),
        'current': ("CURRENT", _fmt_time(elapsed),             text_col),
        'delta':   ("DELTA",   delta_txt,                      delta_col),
    }
    selected = data.get('selected_fields') or ['lap', 'best', 'current', 'delta']
    rows = [all_rows[k] for k in selected if k in all_rows]
    if not rows:
        rows = [("—", "—", label_col)]
    align = str(data.get('text_align', 'split')).lower()
    if align not in ('split', 'left', 'center', 'right'):
        align = 'split'

    # ── Figure ────────────────────────────────────────────────────────────────
    dpi = 100
    fig = plt.figure(figsize=(w / dpi, h / dpi), dpi=dpi)
    fig.patch.set_alpha(0)

    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_facecolor((0, 0, 0, 0))
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis('off')

    # Background pill
    ax.add_patch(FancyBboxPatch((0.02, 0.02), 0.96, 0.96,
        boxstyle='round,pad=0.025',
        facecolor=bg_rgba, edgecolor=bg_edge, linewidth=0.8, zorder=1))

    # Left accent bar
    ax.plot([0.035, 0.035], [0.08, 0.92],
            color=fill_pos, lw=2.5, solid_capstyle='round', zorder=3)

    # Row geometry — tight, fills the background
    n        = len(rows)
    y_top    = 0.94
    y_bottom = 0.06
    row_h    = (y_top - y_bottom) / n

    # Thin horizontal dividers between rows (except last)
    for i in range(1, n):
        yy = y_top - row_h * i
        ax.plot([0.06, 0.97], [yy, yy],
                color=bg_edge, lw=0.5, zorder=2)

    # Font sizes driven purely by widget height so text fills each row.
    # matplotlib pts at 100 dpi: 1 pt ≈ 1.39 px  →  px = h * row_frac
    # label ≈ 28% of row height, value ≈ 52% of row height
    fs_label = max(5,  int(h * row_h * 0.28 / 1.39))
    fs_value = max(7,  int(h * row_h * 0.52 / 1.39))

    pad_l = 0.08

    for i, (lbl, val, col) in enumerate(rows):
        # Each row is split: small label in upper ~35%, large value in lower 65%
        yc    = y_top - row_h * (i + 0.5)
        y_lbl = yc + row_h * 0.20
        y_val = yc - row_h * 0.12

        x_lbl = 0.50 if align == 'center' else (0.97 if align == 'right' else pad_l)
        ha_lbl = 'center' if align == 'center' else ('right' if align == 'right' else 'left')
        ax.text(x_lbl, y_lbl, lbl,
                ha=ha_lbl, va='center', color=label_col,
                fontsize=fs_label, fontfamily='sans-serif', zorder=4)
        if align == 'split':
            x_val, ha_val = 0.97, 'right'
        elif align == 'center':
            x_val, ha_val = 0.50, 'center'
        elif align == 'right':
            x_val, ha_val = 0.97, 'right'
        else:
            x_val, ha_val = pad_l, 'left'
        ax.text(x_val, y_val, val,
                ha=ha_val, va='center', color=col,
                fontsize=fs_value, fontweight='bold',
                fontfamily='monospace', zorder=4)

    return fig_to_rgba(fig, (w, h))
