'use strict';

/** Selection metadata drawn only onto exported map images. */
(() => {
  const MODEL_NAMES = {
    hrrr: 'HRRR', nam3km: 'NAM 3 km', nam12km: 'NAM 12 km', rap: 'RAP',
    gfs: 'GFS', gefs: 'GEFS', ecmwf: 'ECMWF', gdps: 'GDPS',
    href: 'HREF Ensemble', rrfs: 'RRFS', rtma: 'RTMA',
  };
  const utc = (date) => date.toISOString().slice(0, 16).replace('T', ' ') + ' UTC';

  /** Build forecast details without depending on hidden map-info elements. */
  function model({ model: name, product, runId, fxx }) {
    const run = String(runId || '');
    let init = null;
    if (/^\d{10}$/.test(run)) {
      const iso = `${run.slice(0,4)}-${run.slice(4,6)}-${run.slice(6,8)}T${run.slice(8,10)}:00:00Z`;
      const date = new Date(iso);
      if (Number.isFinite(date.getTime()) && date.toISOString().slice(0, 13) === iso.slice(0, 13)) init = date;
    }
    const frame = String(fxx ?? '').match(/^(\d+)(?:_(\d+)min)?$/);
    const minutes = frame ? Number(frame[1]) * 60 + Number(frame[2] || 0) : null;
    const forecast = minutes === null ? '--' : `F+${String(Math.floor(minutes / 60)).padStart(2,'0')}${minutes % 60 ? ':' + String(minutes % 60).padStart(2,'0') : ''}`;
    const valid = init && minutes !== null ? new Date(init.getTime() + minutes * 60000) : null;
    return [
      [MODEL_NAMES[name] || String(name || 'Model').toUpperCase(), product].filter(Boolean).join(' · '),
      `Run: ${init ? utc(init) : '--'} · ${forecast}`,
      `Valid: ${valid ? utc(valid) : '--'}`,
    ].join('\n');
  }

  /** Wrap the caption to the output width so phone exports retain every field. */
  function layout(ctx, text, width) {
    const maximum = Math.max(1, width - 48);
    const lines = [];
    ctx.save();
    ctx.font = '600 12px Arial';
    for (const row of String(text).split('\n').filter(Boolean)) {
      let line = '';
      for (const word of row.split(/\s+/)) {
        const next = line ? `${line} ${word}` : word;
        if (line && ctx.measureText(next).width > maximum) {
          lines.push(line);
          line = word;
        } else line = next;
      }
      if (line) lines.push(line);
    }
    const boxWidth = Math.min(width - 24, Math.max(...lines.map(line => ctx.measureText(line).width), 0) + 24);
    ctx.restore();
    return { lines, width: boxWidth, height: lines.length * 18 + 20 };
  }

  function height(ctx, text, width) { return text ? layout(ctx, text, width).height : 0; }

  /** Reset the capture transform before placing a readable caption inside the PNG. */
  function stamp(canvas, text, { scale = 1, bottomOffset = 0 } = {}) {
    if (!text) return canvas;
    const ctx = canvas.getContext('2d');
    if (!ctx) return canvas;
    const width = canvas.width / scale;
    const imageHeight = canvas.height / scale;
    const box = layout(ctx, text, width);
    const x = width - box.width - 12;
    const y = imageHeight - box.height - 12 - bottomOffset;
    ctx.save();
    ctx.setTransform(scale, 0, 0, scale, 0, 0);
    ctx.globalAlpha = 1;
    ctx.globalCompositeOperation = 'source-over';
    ctx.shadowBlur = 0;
    ctx.shadowOffsetX = 0;
    ctx.shadowOffsetY = 0;
    ctx.fillStyle = 'rgba(7,10,16,0.88)';
    ctx.strokeStyle = 'rgba(152,184,228,0.3)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.roundRect(x, y, box.width, box.height, 8);
    ctx.fill();
    ctx.stroke();
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    box.lines.forEach((line, index) => {
      ctx.font = `${index === 0 ? 600 : 400} 12px Arial`;
      ctx.fillStyle = index === 0 ? '#7dd3e8' : '#e8eef5';
      ctx.fillText(line, x + 12, y + 19 + index * 18);
    });
    ctx.restore();
    return canvas;
  }

  window.MapCaptureMetadata = { model, height, stamp };
})();
