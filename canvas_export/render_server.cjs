'use strict';
/**
 * stdin: one JSON object per line (UTF-8) — same shape as Python ``build_canvas_paint_plan``.
 * stdout: 12 bytes LE uint32 ×3 = width, height, byteLength, then raw RGBA8888 row-major.
 */
const readline = require('readline');
const { renderOverlayFrame } = require('./gauge_bundle.cjs');

const rl = readline.createInterface({ input: process.stdin, terminal: false });

rl.on('line', async (line) => {
  const t = String(line || '').trim();
  if (!t) return;
  let plan;
  try {
    plan = JSON.parse(t);
  } catch (e) {
    process.stderr.write(`canvas_export: invalid JSON: ${e.message}\n`);
    return;
  }
  try {
    const cvs = await renderOverlayFrame(plan);
    const w = cvs.width | 0;
    const h = cvs.height | 0;
    const ctx = cvs.getContext('2d');
    const img = ctx.getImageData(0, 0, w, h);
    const buf = Buffer.from(img.data.buffer, img.data.byteOffset, img.data.byteLength);
    const hdr = Buffer.alloc(12);
    hdr.writeUInt32LE(w >>> 0, 0);
    hdr.writeUInt32LE(h >>> 0, 4);
    hdr.writeUInt32LE(buf.length >>> 0, 8);
    process.stdout.write(hdr);
    process.stdout.write(buf);
  } catch (e) {
    process.stderr.write(`canvas_export: render error: ${e && e.stack ? e.stack : e}\n`);
  }
});
