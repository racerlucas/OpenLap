/**
 * delta_bar.js — iRacing/RaceChrono-like delta bar.
 *
 * Centered zero line:
 * - negative (faster) fills to left in green
 * - positive (slower) fills to right in red
 */
const GaugeDeltaBar = {
  _NEUTRAL_BAND: 0.10,

  /** Map seconds → [0..1] bar length (half-width scale). Similar to iRacing-style HUDs:
   *  - smaller full-scale (default ±1.0s) so small deltas are visible
   *  - power curve (exponent < 1) boosts small values without instantly maxing out
   */
  _barMagnitude(delta, fullScale = 1.0, exponent = 0.45) {
    const fs = Math.max(0.05, Number(fullScale) || 1.0);
    const u = Math.min(1, Math.abs(delta) / fs);
    return Math.pow(u, Math.max(0.15, Math.min(1, Number(exponent) || 0.45)));
  },

  _colour(delta) {
    if (Math.abs(delta) <= this._NEUTRAL_BAND) return '#e8e8e8';
    return delta < 0 ? '#22dd66' : '#ff4444';
  },

  render(ctx, data, w, h) {
    const theme = GaugeBase.getTheme(data);
    GaugeBase.drawBackground(ctx, w, h, theme);

    const hasValue = data.value != null && Number.isFinite(data.value);
    const value = hasValue ? Number(data.value) : 0;
    const label = (data.label || 'Delta').toUpperCase();
    const fullScale = Number(data.delta_bar_full_scale ?? 1.0);
    const curveExp = Number(data.delta_bar_curve ?? 0.45);

    const sc = Math.sqrt((w / 180) * (h / 120));
    const fsLabel = Math.max(8, Math.min(Math.round(10 * sc), Math.round(w * 0.08)));
    const fsVal = Math.max(9, Math.min(Math.round(15 * sc), Math.round(w * 0.12)));

    const pad = w * 0.08;
    const x0 = pad;
    const x1 = w - pad;
    const bw = x1 - x0;
    const by = h * 0.42;
    const bh = h * 0.20;
    const cx = (x0 + x1) * 0.5;

    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillStyle = theme.label;
    ctx.font = `${fsLabel}px 'Segoe UI', sans-serif`;
    ctx.fillText(label, w * 0.5, h * 0.84);

    ctx.fillStyle = theme.track;
    ctx.beginPath();
    GaugeBase.roundRect(ctx, x0, by, bw, bh, 2);
    ctx.fill();

    const mag = hasValue ? this._barMagnitude(value, fullScale, curveExp) : 0;
    const sign = value > 0 ? 1 : (value < 0 ? -1 : 0);
    const frac = sign * mag;
    if (frac > 0) {
      ctx.fillStyle = '#ff4444';
      ctx.globalAlpha = 0.9;
      ctx.fillRect(cx, by, (bw * 0.5) * frac, bh);
      ctx.globalAlpha = 1;
    } else if (frac < 0) {
      ctx.fillStyle = '#22dd66';
      ctx.globalAlpha = 0.9;
      ctx.fillRect(cx + (bw * 0.5) * frac, by, (bw * 0.5) * (-frac), bh);
      ctx.globalAlpha = 1;
    }

    ctx.strokeStyle = '#3a4a5a';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(cx, by - 2);
    ctx.lineTo(cx, by + bh + 2);
    ctx.stroke();

    const text = hasValue ? ((value >= 0 ? '+' : '\u2212') + Math.abs(value).toFixed(3)) : '—';
    ctx.fillStyle = hasValue ? this._colour(value) : theme.label;
    ctx.font = `bold ${fsVal}px 'Segoe UI', sans-serif`;
    ctx.fillText(text, w * 0.5, h * 0.25);
  }
};
