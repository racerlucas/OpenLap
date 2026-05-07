"""
Gauge style: Info
=================
Session info text panel — shows static metadata (track, date/time, vehicle,
session type) plus the live exhaust temperature when available.
Empty / zero fields are hidden automatically.

ELEMENT_TYPE : "gauge"
STYLE_NAME   : "Info"
Data keys    : info_track, info_date, info_time, info_vehicle, info_session,
               info_source, exhaust_temp
"""
STYLE_NAME   = 'Info'
ELEMENT_TYPE = 'gauge'

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch


def render(data: dict, w: int, h: int):
    from overlay_utils import fig_to_rgba, px_to_pt, font_px_to_pt

    T         = data.get('_tc', {})
    bg_rgba   = T.get('bg_rgba',      (0.04, 0.06, 0.10, 0.78))
    bg_edge   = T.get('bg_edge_rgba', (1.00, 1.00, 1.00, 0.08))
    text_col  = T.get('text',         'white')
    label_col = T.get('label',        '#445566')
    fill_pos  = T.get('fill_pos',     '#ffaa00')

    from gauge_channels import INFO_FIELDS_DEFAULT

    # Which fields to render — comes from the gauge's channel list, or default set
    selected: list[str] = data.get('selected_fields') or INFO_FIELDS_DEFAULT

    # Build value lookup — selected fields that are empty show "—" as placeholder
    # so the layout reflects exactly what has been enabled.
    _values: dict[str, tuple[str, str]] = {}

    track = data.get('info_track', '')
    _values['track'] = ('TRACK', track or '—')

    date_s = data.get('info_date', '')
    time_s = data.get('info_time', '')
    if date_s and time_s:
        _values['datetime'] = ('DATE', f"{date_s}  {time_s}")
    elif date_s:
        _values['datetime'] = ('DATE', date_s)
    else:
        _values['datetime'] = ('DATE', '—')

    vehicle = data.get('info_vehicle', '')
    _values['vehicle'] = ('VEHICLE', vehicle or '—')

    session_t = data.get('info_session', '')
    _values['session'] = ('SESSION', session_t or '—')

    weather = data.get('info_weather', '')
    _values['weather'] = ('WEATHER', weather or '—')

    wind = data.get('info_wind', '')
    _values['wind'] = ('WIND', wind or '—')

    fields = [_values[k] for k in selected if k in _values]
    if not fields:
        fields = [('INFO', '—')]
    align = str(data.get('text_align', 'left')).lower()
    if align not in ('left', 'center', 'right'):
        align = 'left'

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

    # Thin accent bar on the left edge
    ax.plot([0.035, 0.035], [0.08, 0.92],
            color=fill_pos, lw=px_to_pt(2.5, dpi), solid_capstyle='round', zorder=3)

    n        = len(fields)
    pad_l    = 0.08
    x_text = 0.50 if align == 'center' else (0.92 if align == 'right' else pad_l)
    y_top    = 0.94
    y_bottom = 0.06
    row_h    = (y_top - y_bottom) / max(n, 1)

    fs_label = max(4, int(h * row_h * 0.26))
    fs_value = max(5, int(h * row_h * 0.48))

    for i, (lbl, val) in enumerate(fields):
        yc    = y_top - row_h * (i + 0.5)
        y_lbl = yc + row_h * 0.20
        y_val = yc - row_h * 0.14

        ax.text(x_text, y_lbl, lbl,
                ha=align, va='center', color=label_col,
                fontsize=font_px_to_pt(fs_label, dpi), zorder=4)
        ax.text(x_text, y_val, val,
                ha=align, va='center', color=text_col,
                fontsize=font_px_to_pt(fs_value, dpi), fontweight='bold', zorder=4)

    return fig_to_rgba(fig, (w, h))
