'use strict';

/**
 * Point Forecast — multi-model meteogram plume for the desktop viewer.
 *
 * Classic deferred script loaded after app.js. It relies on app.js / main.js
 * globals (state, leafletMap, BASE, apiFetch, setSoundingProbeEnabled,
 * closeSoundingPanel, formatLatLonLabel) and publishes window.WXMeteogram.
 */
(function meteogramModule() {
  const SVG_NS = 'http://www.w3.org/2000/svg';
  const HOUR_MS = 3600 * 1000;
  const CT_ZONE = 'America/Chicago';

  // Dark-surface categorical slots, fixed per model so toggling never repaints.
  const MODEL_COLORS = {
    hrrr: '#3987e5', rrfs: '#d95926', nam3km: '#199e70', rap: '#c98500',
    nam12km: '#d55181', href: '#008300', gfs: '#9085e9', gefs: '#e66767',
  };
  // Row labels for the precipitation strip, where the full name will not fit.
  const SHORT_LABELS = {
    hrrr: 'HRRR', rrfs: 'RRFS', nam3km: 'NAM3', rap: 'RAP',
    nam12km: 'NAM12', href: 'HREF', gfs: 'GFS', gefs: 'GEFS',
  };
  const RANGES = [
    { label: '48h', hours: 48 },
    { label: '84h', hours: 84 },
    { label: '7d', hours: 168 },
    { label: '16d', hours: 384 },
  ];
  const LINE_CHARTS = [
    { param: 'temp_2m', title: 'Temperature', unit: '°F', spread: true, refLine: 32 },
    { param: 'dewpoint_2m', title: 'Dewpoint', unit: '°F', spread: true },
    { param: 'wind_gusts', title: 'Wind gusts', unit: 'mph', floorZero: true },
    { param: 'cloud_cover', title: 'Cloud cover', unit: '%', fixed: [0, 100] },
  ];
  const MARGIN = { top: 8, right: 14, bottom: 22, left: 46 };
  const LINE_HEIGHT = 150;
  const PRECIP_ROW = 14;

  const mg = {
    enabled: false,
    point: null,
    models: [],
    results: new Map(),   // model -> response | {error}
    hidden: new Set(),
    rangeHours: 84,
    requestSeq: 0,
    marker: null,
    precipScale: null,
    renderQueued: false,
    domain: null,
  };

  const $ = (id) => document.getElementById(id);
  const els = {
    button: $('meteogramProbeBtn'),
    panel: $('meteogramPanel'),
    title: $('meteogramTitle'),
    meta: $('meteogramMeta'),
    status: $('meteogramStatus'),
    range: $('meteogramRange'),
    legend: $('meteogramLegend'),
    charts: $('meteogramCharts'),
    tooltip: $('meteogramTooltip'),
    close: $('meteogramClose'),
  };
  if (!els.panel || !els.button) return;

  // ---------------------------------------------------------------------------
  // Time helpers (all labels in Central Time, like the rest of the viewer)
  // ---------------------------------------------------------------------------

  const ctParts = new Intl.DateTimeFormat('en-US', {
    timeZone: CT_ZONE, weekday: 'short', day: 'numeric', hour: 'numeric', hour12: false,
  });

  /** Central Time weekday, day-of-month and hour (0–23) for a timestamp. */
  function ctFields(ms) {
    const out = {};
    for (const part of ctParts.formatToParts(new Date(ms))) out[part.type] = part.value;
    return { weekday: out.weekday, day: out.day, hour: Number(out.hour) % 24 };
  }

  const tooltipTime = new Intl.DateTimeFormat('en-US', {
    timeZone: CT_ZONE, weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', hour12: true,
  });

  function runLabel(runId) {
    return runId ? `${runId.slice(8, 10)}z ${runId.slice(4, 6)}/${runId.slice(6, 8)}` : '';
  }

  // ---------------------------------------------------------------------------
  // Data shaping
  // ---------------------------------------------------------------------------

  /** [{t, v}] for a model/param, sorted by valid time. */
  function seriesFor(result, param) {
    const raw = result?.series?.[param];
    if (!raw) return [];
    const init = Date.parse(result.init_time);
    if (!Number.isFinite(init)) return [];
    return raw
      .filter(([, v]) => v !== null && Number.isFinite(v))
      .map(([fxx, v]) => ({ t: init + fxx * HOUR_MS, v }));
  }

  /** Linear interpolation inside a series; null outside it or across long gaps. */
  function interpolate(points, t) {
    if (!points.length || t < points[0].t || t > points[points.length - 1].t) return null;
    let lo = 0;
    let hi = points.length - 1;
    while (hi - lo > 1) {
      const mid = (lo + hi) >> 1;
      if (points[mid].t <= t) lo = mid; else hi = mid;
    }
    const a = points[lo];
    const b = points[hi];
    if (a.t === t) return a.v;
    if (b.t === t) return b.v;
    if (b.t - a.t > 12 * HOUR_MS) return null;
    return a.v + (b.v - a.v) * ((t - a.t) / (b.t - a.t));
  }

  function median(values) {
    const sorted = [...values].sort((x, y) => x - y);
    const mid = sorted.length >> 1;
    return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
  }

  function niceTicks(min, max, count = 4) {
    const span = max - min || 1;
    const raw = span / count;
    const mag = 10 ** Math.floor(Math.log10(raw));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) || raw;
    const ticks = [];
    for (let v = Math.ceil(min / step) * step; v <= max + step * 1e-6; v += step) {
      ticks.push(Math.round(v * 1000) / 1000);
    }
    return ticks;
  }

  function visibleModels() {
    return mg.models.filter((m) => !mg.hidden.has(m.model) && mg.results.get(m.model)?.series);
  }

  // ---------------------------------------------------------------------------
  // SVG helpers
  // ---------------------------------------------------------------------------

  function svgEl(name, attrs = {}, parent = null) {
    const node = document.createElementNS(SVG_NS, name);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
    if (parent) parent.appendChild(node);
    return node;
  }

  function svgText(parent, x, y, text, attrs = {}) {
    const node = svgEl('text', { x, y, ...attrs }, parent);
    node.textContent = text;
    return node;
  }

  /** Day separators, hour grid and labels shared by every chart. */
  function drawTimeAxis(svg, x, plotTop, plotBottom) {
    const [t0, t1] = mg.domain;
    const grid = svgEl('g', { class: 'mg-grid' }, svg);
    const showHours = mg.rangeHours <= 84;
    const labelEvery = mg.rangeHours > 168 ? 2 : 1;
    let dayIndex = 0;
    for (let t = Math.ceil(t0 / HOUR_MS) * HOUR_MS; t <= t1; t += HOUR_MS) {
      const { hour, weekday, day } = ctFields(t);
      const px = x(t);
      if (hour === 0) {
        svgEl('line', { x1: px, x2: px, y1: plotTop, y2: plotBottom, class: 'mg-day-line' }, grid);
        if (dayIndex++ % labelEvery === 0) {
          svgText(grid, px + 3, plotBottom + 14, `${weekday} ${day}`, { class: 'mg-axis-label' });
        }
      } else if (showHours && hour % 6 === 0) {
        svgEl('line', { x1: px, x2: px, y1: plotTop, y2: plotBottom, class: 'mg-hour-line' }, grid);
        if (mg.rangeHours <= 48) {
          svgText(grid, px, plotBottom + 14, hour === 12 ? '12p' : hour < 12 ? `${hour}a` : `${hour - 12}p`,
            { class: 'mg-axis-label mg-axis-label--minor', 'text-anchor': 'middle' });
        }
      }
    }
    const now = Date.now();
    if (now >= t0 && now <= t1) {
      svgEl('line', { x1: x(now), x2: x(now), y1: plotTop, y2: plotBottom, class: 'mg-now-line' }, grid);
    }
  }

  function chartSection(title, note) {
    const section = document.createElement('section');
    section.className = 'sounding-panel-section meteogram-section';
    const header = document.createElement('div');
    header.className = 'sounding-section-header';
    const titleEl = document.createElement('div');
    titleEl.className = 'sounding-section-title';
    titleEl.textContent = title;
    const noteEl = document.createElement('div');
    noteEl.className = 'sounding-section-note';
    noteEl.textContent = note;
    header.append(titleEl, noteEl);
    section.appendChild(header);
    els.charts.appendChild(section);
    return section;
  }

  // ---------------------------------------------------------------------------
  // Charts
  // ---------------------------------------------------------------------------

  function renderLineChart(spec, width) {
    const models = visibleModels()
      .map((m) => ({ ...m, points: seriesFor(mg.results.get(m.model), spec.param) }))
      .filter((m) => m.points.length);
    if (!models.length) return;
    const [t0, t1] = mg.domain;
    const inRange = (p) => p.t >= t0 - 6 * HOUR_MS && p.t <= t1 + 6 * HOUR_MS;

    // Hourly consensus (median and min–max envelope) where >= 3 models overlap.
    const consensus = [];
    if (spec.spread) {
      for (let t = Math.ceil(t0 / HOUR_MS) * HOUR_MS; t <= t1; t += HOUR_MS) {
        const values = models.map((m) => interpolate(m.points, t)).filter((v) => v !== null);
        if (values.length >= 3) {
          consensus.push({ t, min: Math.min(...values), max: Math.max(...values), med: median(values) });
        }
      }
    }

    let lo;
    let hi;
    if (spec.fixed) {
      [lo, hi] = spec.fixed;
    } else {
      const all = models.flatMap((m) => m.points.filter((p) => p.t >= t0 && p.t <= t1).map((p) => p.v));
      if (!all.length) return;
      lo = Math.min(...all);
      hi = Math.max(...all);
      const pad = Math.max((hi - lo) * 0.08, 2);
      lo = spec.floorZero ? Math.max(0, lo - pad) : lo - pad;
      hi += pad;
    }
    const ticks = niceTicks(lo, hi);
    lo = Math.min(lo, ticks[0]);
    hi = Math.max(hi, ticks[ticks.length - 1]);

    const section = chartSection(spec.title, spec.unit);
    const height = LINE_HEIGHT;
    const plotTop = MARGIN.top;
    const plotBottom = height - MARGIN.bottom;
    const x = (t) => MARGIN.left + ((t - t0) / (t1 - t0)) * (width - MARGIN.left - MARGIN.right);
    const y = (v) => plotBottom - ((v - lo) / (hi - lo)) * (plotBottom - plotTop);

    const svg = svgEl('svg', {
      class: 'meteogram-chart', width, height, viewBox: `0 0 ${width} ${height}`,
      role: 'img', 'aria-label': `${spec.title} forecast by model`,
    }, section);
    svgEl('clipPath', { id: `mg-clip-${spec.param}` }, svg)
      .appendChild(svgEl('rect', { x: MARGIN.left, y: plotTop - 2, width: width - MARGIN.left - MARGIN.right, height: plotBottom - plotTop + 4 }));

    const axis = svgEl('g', { class: 'mg-grid' }, svg);
    for (const tick of ticks) {
      svgEl('line', { x1: MARGIN.left, x2: width - MARGIN.right, y1: y(tick), y2: y(tick), class: 'mg-value-line' }, axis);
      svgText(axis, MARGIN.left - 6, y(tick) + 3, `${tick}`, { class: 'mg-axis-label', 'text-anchor': 'end' });
    }
    if (spec.refLine !== undefined && spec.refLine > lo && spec.refLine < hi) {
      svgEl('line', { x1: MARGIN.left, x2: width - MARGIN.right, y1: y(spec.refLine), y2: y(spec.refLine), class: 'mg-ref-line' }, axis);
    }
    drawTimeAxis(svg, x, plotTop, plotBottom);

    const plot = svgEl('g', { 'clip-path': `url(#mg-clip-${spec.param})` }, svg);
    if (consensus.length > 1) {
      const upper = consensus.map((c) => `${x(c.t).toFixed(1)},${y(c.max).toFixed(1)}`);
      const lower = consensus.slice().reverse().map((c) => `${x(c.t).toFixed(1)},${y(c.min).toFixed(1)}`);
      svgEl('polygon', { points: [...upper, ...lower].join(' '), class: 'mg-spread' }, plot);
    }
    for (const model of models) {
      const segments = [];
      let current = [];
      let prev = null;
      for (const p of model.points.filter(inRange)) {
        if (prev && p.t - prev.t > 12 * HOUR_MS && current.length) {
          segments.push(current);
          current = [];
        }
        current.push(`${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`);
        prev = p;
      }
      if (current.length) segments.push(current);
      for (const segment of segments) {
        svgEl('polyline', { points: segment.join(' '), class: 'mg-series', stroke: MODEL_COLORS[model.model] || '#c6cbd4' }, plot);
      }
    }
    if (consensus.length > 1) {
      svgEl('polyline', {
        points: consensus.map((c) => `${x(c.t).toFixed(1)},${y(c.med).toFixed(1)}`).join(' '),
        class: 'mg-median',
      }, plot);
    }

    attachCrosshair(svg, {
      x, plotTop, plotBottom, width,
      rows: (t) => {
        const rows = models.map((m) => ({ model: m, value: interpolateExact(m.points, t) }));
        const c = consensus.find((entry) => entry.t === t);
        return { rows, footer: c ? `Median ${c.med.toFixed(0)} · range ${c.min.toFixed(0)}–${c.max.toFixed(0)} ${spec.unit}` : '' };
      },
      format: (v) => `${v.toFixed(spec.unit === '%' ? 0 : spec.unit === 'mph' ? 0 : 1)} ${spec.unit}`,
    });
  }

  /** Value at an exact model time step, else interpolated within 3 hours. */
  function interpolateExact(points, t) {
    const exact = points.find((p) => p.t === t);
    if (exact) return exact.v;
    const before = points.filter((p) => p.t < t).pop();
    const after = points.find((p) => p.t > t);
    if (!before || !after || after.t - before.t > 6 * HOUR_MS) return null;
    return interpolate(points, t);
  }

  function precipColor(value) {
    const scale = mg.precipScale;
    if (!scale || !(value > 0)) return null;
    let index = 0;
    for (let i = 0; i < scale.bounds.length - 1; i += 1) {
      if (value >= scale.bounds[i]) index = i;
    }
    return scale.colors[Math.min(index, scale.colors.length - 1)];
  }

  function precipBin(value) {
    const scale = mg.precipScale;
    if (!scale) return `${value.toFixed(2)} in`;
    for (let i = 0; i < scale.bounds.length - 1; i += 1) {
      if (value >= scale.bounds[i] && value < scale.bounds[i + 1]) {
        return `${scale.bounds[i]}–${scale.bounds[i + 1]} in`;
      }
    }
    return `≥ ${scale.bounds[scale.bounds.length - 1]} in`;
  }

  function renderPrecipStrip(width) {
    const models = visibleModels()
      .map((m) => ({ ...m, result: mg.results.get(m.model), points: seriesFor(mg.results.get(m.model), 'precip') }))
      .filter((m) => m.points.length);
    if (!models.length) return;
    const [t0, t1] = mg.domain;
    const section = chartSection('Precipitation timing', 'shade = rendered QPF bin');
    const plotTop = MARGIN.top;
    const plotBottom = plotTop + models.length * PRECIP_ROW;
    const height = plotBottom + MARGIN.bottom;
    const x = (t) => MARGIN.left + ((t - t0) / (t1 - t0)) * (width - MARGIN.left - MARGIN.right);
    const svg = svgEl('svg', {
      class: 'meteogram-chart', width, height, viewBox: `0 0 ${width} ${height}`,
      role: 'img', 'aria-label': 'Precipitation timing by model',
    }, section);
    drawTimeAxis(svg, x, plotTop, plotBottom);
    const cells = svgEl('g', {}, svg);
    models.forEach((model, row) => {
      const top = plotTop + row * PRECIP_ROW;
      svgEl('line', { x1: MARGIN.left, x2: width - MARGIN.right, y1: top + PRECIP_ROW - 0.5, y2: top + PRECIP_ROW - 0.5, class: 'mg-row-line' }, cells);
      svgText(cells, MARGIN.left - 6, top + PRECIP_ROW - 4, SHORT_LABELS[model.model] || model.label, { class: 'mg-axis-label', 'text-anchor': 'end' });
      model.points.forEach((p, i) => {
        const fill = precipColor(p.v);
        if (!fill) return;
        // Each frame covers the window since the previous frame.
        const start = i > 0 ? model.points[i - 1].t : p.t - HOUR_MS;
        if (p.t < t0 || start > t1) return;
        const left = x(Math.max(start, t0));
        const right = x(Math.min(p.t, t1));
        svgEl('rect', {
          x: left + 0.5, y: top + 2, width: Math.max(1, right - left - 1), height: PRECIP_ROW - 4,
          rx: 2, fill, class: 'mg-precip-cell',
        }, cells);
      });
    });
    attachCrosshair(svg, {
      x, plotTop, plotBottom, width,
      rows: (t) => ({
        rows: models.map((m) => {
          const hit = m.points.find((p, i) => {
            const start = i > 0 ? m.points[i - 1].t : p.t - HOUR_MS;
            return t > start && t <= p.t;
          });
          return { model: m, value: hit ? hit.v : null, window: hit && !m.result.precip_hourly };
        }),
        footer: 'HRRR and RAP are hourly amounts; other models show their accumulation window.',
      }),
      format: (v, row) => (v > 0 ? `${precipBin(v)}${row.window ? ' (window)' : ''}` : 'dry'),
    });
  }

  // ---------------------------------------------------------------------------
  // Crosshair + tooltip
  // ---------------------------------------------------------------------------

  function attachCrosshair(svg, opts) {
    const line = svgEl('line', { y1: opts.plotTop, y2: opts.plotBottom, class: 'mg-crosshair', visibility: 'hidden' }, svg);
    const hit = svgEl('rect', {
      x: MARGIN.left, y: 0, width: opts.width - MARGIN.left - MARGIN.right, height: opts.plotBottom,
      fill: 'transparent', class: 'mg-hit',
    }, svg);
    const [t0, t1] = mg.domain;
    function onMove(evt) {
      const rect = svg.getBoundingClientRect();
      const px = evt.clientX - rect.left;
      const raw = t0 + ((px - MARGIN.left) / (opts.width - MARGIN.left - MARGIN.right)) * (t1 - t0);
      const t = Math.round(raw / HOUR_MS) * HOUR_MS;
      const cx = opts.x(t);
      line.setAttribute('x1', cx);
      line.setAttribute('x2', cx);
      line.setAttribute('visibility', 'visible');
      showTooltip(evt, t, opts);
    }
    function onLeave() {
      line.setAttribute('visibility', 'hidden');
      els.tooltip.hidden = true;
    }
    hit.addEventListener('pointermove', onMove);
    hit.addEventListener('pointerleave', onLeave);
  }

  function showTooltip(evt, t, opts) {
    const { rows, footer } = opts.rows(t);
    const tip = els.tooltip;
    tip.replaceChildren();
    const head = document.createElement('div');
    head.className = 'mg-tip-time';
    head.textContent = `${tooltipTime.format(new Date(t))} CT`;
    tip.appendChild(head);
    for (const row of rows) {
      const line = document.createElement('div');
      line.className = 'mg-tip-row';
      const key = document.createElement('span');
      key.className = 'mg-tip-key';
      key.style.background = MODEL_COLORS[row.model.model] || '#c6cbd4';
      const value = document.createElement('span');
      value.className = 'mg-tip-value';
      value.textContent = row.value === null ? '—' : opts.format(row.value, row);
      const name = document.createElement('span');
      name.className = 'mg-tip-name';
      name.textContent = row.model.label;
      line.append(key, value, name);
      tip.appendChild(line);
    }
    if (footer) {
      const foot = document.createElement('div');
      foot.className = 'mg-tip-footer';
      foot.textContent = footer;
      tip.appendChild(foot);
    }
    tip.hidden = false;
    const panelRect = els.panel.getBoundingClientRect();
    const tipRect = tip.getBoundingClientRect();
    let left = evt.clientX - panelRect.left + els.panel.scrollLeft + 14;
    if (left + tipRect.width > panelRect.width - 8) left -= tipRect.width + 28;
    let top = evt.clientY - panelRect.top + els.panel.scrollTop + 12;
    const maxTop = els.panel.scrollTop + panelRect.height - tipRect.height - 8;
    top = Math.min(top, maxTop);
    tip.style.left = `${Math.max(8, left)}px`;
    tip.style.top = `${Math.max(8, top)}px`;
  }

  // ---------------------------------------------------------------------------
  // Panel chrome
  // ---------------------------------------------------------------------------

  function renderRangeButtons() {
    els.range.replaceChildren();
    for (const range of RANGES) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'meteogram-range-btn';
      btn.textContent = range.label;
      btn.setAttribute('aria-pressed', range.hours === mg.rangeHours ? 'true' : 'false');
      btn.addEventListener('click', function onRangeClick() {
        mg.rangeHours = range.hours;
        renderRangeButtons();
        scheduleRender();
      });
      els.range.appendChild(btn);
    }
  }

  function renderLegend() {
    els.legend.replaceChildren();
    for (const model of mg.models) {
      const result = mg.results.get(model.model);
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'meteogram-chip';
      const key = document.createElement('span');
      key.className = 'meteogram-chip-key';
      key.style.background = MODEL_COLORS[model.model] || '#c6cbd4';
      const label = document.createElement('span');
      label.textContent = model.label;
      const run = document.createElement('span');
      run.className = 'meteogram-chip-run';
      chip.append(key, label, run);
      if (!result) {
        run.textContent = '…';
        chip.disabled = true;
      } else if (result.error) {
        run.textContent = result.error;
        chip.disabled = true;
        chip.classList.add('is-unavailable');
      } else {
        run.textContent = runLabel(result.run_id);
        const off = mg.hidden.has(model.model);
        chip.classList.toggle('is-off', off);
        chip.setAttribute('aria-pressed', off ? 'false' : 'true');
        chip.title = `${model.label} run ${result.run_id} — click to ${off ? 'show' : 'hide'}`;
        chip.addEventListener('click', function onChipClick() {
          if (mg.hidden.has(model.model)) mg.hidden.delete(model.model); else mg.hidden.add(model.model);
          scheduleRender();
        });
      }
      els.legend.appendChild(chip);
    }
  }

  function setStatus(message) {
    els.status.textContent = message || '';
    els.status.classList.toggle('hidden', !message);
  }

  function render() {
    mg.renderQueued = false;
    renderLegend();
    els.charts.replaceChildren();
    els.tooltip.hidden = true;
    const start = Math.floor((Date.now() - 3 * HOUR_MS) / HOUR_MS) * HOUR_MS;
    mg.domain = [start, start + mg.rangeHours * HOUR_MS];
    const width = Math.max(320, Math.floor(els.charts.clientWidth - 24));
    for (const spec of LINE_CHARTS) renderLineChart(spec, width);
    renderPrecipStrip(width);

    const pending = mg.models.filter((m) => !mg.results.has(m.model)).length;
    const loaded = mg.models.filter((m) => mg.results.get(m.model)?.series).length;
    if (pending) {
      setStatus(`Loading ${pending} of ${mg.models.length} models…`);
    } else if (!loaded) {
      setStatus('No model covers this point.');
    } else {
      setStatus('');
    }
  }

  function scheduleRender() {
    if (mg.renderQueued) return;
    mg.renderQueued = true;
    requestAnimationFrame(render);
  }

  // ---------------------------------------------------------------------------
  // Loading
  // ---------------------------------------------------------------------------

  async function fetchModel(model, point, seq) {
    const url = `${BASE}/api/meteogram/${encodeURIComponent(model.model)}?lat=${encodeURIComponent(point.lat.toFixed(4))}&lon=${encodeURIComponent(point.lng.toFixed(4))}`;
    for (let attempt = 0; attempt < 3; attempt += 1) {
      try {
        // A run whose grids are not cached yet is decoded on demand (~15 s).
        const data = await apiFetch(url, { timeoutMs: 90_000 });
        if (seq === mg.requestSeq) mg.results.set(model.model, data);
        break;
      } catch (err) {
        const status = Number.parseInt(String(err?.message || ''), 10);
        if (status === 503 && attempt < 2) {
          await new Promise((resolve) => setTimeout(resolve, 2000));
          continue;
        }
        if (seq === mg.requestSeq) {
          mg.results.set(model.model, { error: status === 404 ? 'n/a here' : 'failed' });
        }
        break;
      }
    }
    if (seq === mg.requestSeq) scheduleRender();
  }

  async function ensureModels() {
    if (!mg.models.length) mg.models = await apiFetch(`${BASE}/api/meteogram/models`);
    if (!mg.precipScale) {
      try {
        const legend = await apiFetch(`${BASE}/api/legend/precip`);
        if (Array.isArray(legend.bounds) && Array.isArray(legend.colors)) {
          mg.precipScale = { bounds: legend.bounds.map(Number), colors: legend.colors };
        }
      } catch {
        // Cells fall back to no shading only if the legend is unavailable.
      }
    }
  }

  async function open(latlng) {
    if (!latlng) return;
    const seq = ++mg.requestSeq;
    mg.point = { lat: latlng.lat, lng: latlng.lng };
    mg.results = new Map();
    if (typeof closeSoundingPanel === 'function') closeSoundingPanel(false);
    els.panel.classList.remove('hidden');
    els.title.textContent = typeof formatLatLonLabel === 'function' ? formatLatLonLabel(mg.point) : `${mg.point.lat.toFixed(2)}, ${mg.point.lng.toFixed(2)}`;
    els.meta.textContent = 'Latest complete run of each model · times in Central · shaded band = model spread, dashed = median';
    placeMarker();
    renderRangeButtons();
    setStatus('Loading models…');
    try {
      await ensureModels();
    } catch {
      setStatus('Point forecasts are unavailable right now.');
      return;
    }
    if (seq !== mg.requestSeq) return;
    scheduleRender();
    await Promise.all(mg.models.map((model) => fetchModel(model, mg.point, seq)));
  }

  function placeMarker() {
    if (typeof leafletMap === 'undefined' || !leafletMap || typeof L === 'undefined') return;
    if (!mg.marker) {
      mg.marker = L.circleMarker([mg.point.lat, mg.point.lng], {
        radius: 6, weight: 2, color: '#ecf1f8', fillColor: '#3987e5', fillOpacity: 0.9,
        interactive: false,
      });
    }
    mg.marker.setLatLng([mg.point.lat, mg.point.lng]);
    if (!leafletMap.hasLayer(mg.marker)) mg.marker.addTo(leafletMap);
  }

  function close() {
    mg.requestSeq += 1;
    els.panel.classList.add('hidden');
    els.tooltip.hidden = true;
    if (mg.marker && typeof leafletMap !== 'undefined' && leafletMap?.hasLayer(mg.marker)) {
      leafletMap.removeLayer(mg.marker);
    }
  }

  function updateButton() {
    els.button.classList.toggle('active', mg.enabled);
    els.button.setAttribute('aria-pressed', mg.enabled ? 'true' : 'false');
    els.button.textContent = mg.enabled ? 'Point Forecast On' : 'Point Forecast';
    const mapEl = typeof leafletMap !== 'undefined' ? leafletMap?.getContainer?.() : null;
    if (mapEl) {
      const active = mg.enabled && state.dataSource === 'models';
      mapEl.classList.toggle('meteogram-probe-active', active);
      if (active) mapEl.style.cursor = 'crosshair';
      else if (!state.soundingProbeEnabled) mapEl.style.cursor = '';
    }
  }

  function setEnabled(enabled) {
    mg.enabled = !!enabled;
    if (mg.enabled && state.soundingProbeEnabled && typeof setSoundingProbeEnabled === 'function') {
      setSoundingProbeEnabled(false);
    }
    updateButton();
    if (!mg.enabled) close();
  }

  els.button.addEventListener('click', function onMeteogramButtonClick() {
    setEnabled(!mg.enabled);
    // The panel stays closed until a map click so it never hides the map
    // the user is about to click; re-enabling restores the last point.
    if (mg.enabled && mg.point && mg.results.size) {
      els.panel.classList.remove('hidden');
      placeMarker();
      scheduleRender();
    }
  });
  els.close.addEventListener('click', function onMeteogramClose() {
    setEnabled(false);
  });
  if (typeof ResizeObserver === 'function') {
    let lastWidth = 0;
    new ResizeObserver(function onChartsResize(entries) {
      const width = Math.round(entries[0].contentRect.width);
      if (width && Math.abs(width - lastWidth) > 8 && mg.results.size && !els.panel.classList.contains('hidden')) {
        lastWidth = width;
        scheduleRender();
      }
    }).observe(els.charts);
  }

  window.WXMeteogram = {
    isEnabled: () => mg.enabled,
    setEnabled,
    open,
  };
})();
