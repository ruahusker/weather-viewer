'use strict';

/* ═══════════════════════════════════════════════════════════════
   Mobile Sounding / Skew-T — ported from desktop app.js
   Renders SVG skew-T chart, hodograph, and key diagnostics.
   ═══════════════════════════════════════════════════════════════ */

// ── Thermodynamic core (SHARPpy-derived) ─────────────────────

const _Z = 273.15;
const _ROCP = 0.28571426;

function _theta(p, tC, p2 = 1000) {
  return ((tC + _Z) * Math.pow((p2 / p), _ROCP)) - _Z;
}

function _wobf(tC) {
  const t = tC - 20;
  if (t <= 0) {
    let n = 1 + t * (-8.841660499999999e-3 + t * (1.4714143e-4 + t * (-9.671989000000001e-7 + t * (-3.2607217e-8 + t * (-3.8598073e-10)))));
    return 15.13 / Math.pow(n, 4);
  }
  let p = t * (4.9618922e-7 + t * (-6.1059365e-9 + t * (3.9401551e-11 + t * (-1.2588129e-13 + t * (1.668828e-16)))));
  p = 1 + t * (3.6182989e-3 + t * (-1.3603273e-5 + p));
  return (29.93 / Math.pow(p, 4)) + (0.96 * t) - 14.8;
}

function _satlift(p, thetamC, conv = 0.1) {
  if (Math.abs(p - 1000) <= 0.001) return thetamC;
  const pwrp = Math.pow((p / 1000), _ROCP);
  let eor = 999, t1 = 0, e1 = 0, t2 = 0, e2 = 0, rate = 1;
  while (Math.abs(eor) > conv) {
    if (eor === 999) {
      t1 = (thetamC + _Z) * pwrp - _Z;
      e1 = _wobf(t1) - _wobf(thetamC);
      rate = 1;
    } else {
      rate = (t2 - t1) / (e2 - e1);
      t1 = t2; e1 = e2;
    }
    t2 = t1 - (e1 * rate);
    e2 = ((t2 + _Z) / pwrp) - _Z;
    e2 += _wobf(t2) - _wobf(e2) - thetamC;
    eor = e2 * rate;
  }
  return t2 - eor;
}

function _wetlift(p, tC, p2) {
  const thetaC = _theta(p, tC, 1000);
  const thetam = thetaC - _wobf(thetaC) + _wobf(tC);
  return _satlift(p2, thetam);
}

function _tempAtMixrat(w, p) {
  if (!Number.isFinite(w) || !Number.isFinite(p) || w <= 0 || p <= 0) return null;
  const x = Math.log10((w * p) / (622 + w));
  return Math.pow(10, (0.0498646455 * x) + 2.4082965) - 7.07475 + (38.9114 * Math.pow(Math.pow(10, 0.0915 * x) - 1.2035, 2)) - _Z;
}

// ── Path builders ────────────────────────────────────────────

function _buildPath(levels, accessor, xFn, yFn) {
  const pts = levels.filter(l => Number.isFinite(accessor(l)))
    .map(l => `${xFn(accessor(l), l.pressure_hpa).toFixed(1)},${yFn(l.pressure_hpa).toFixed(1)}`);
  return pts.length >= 2 ? `M ${pts.join(' L ')}` : '';
}

function _buildThermoPath(points, xFn, yFn) {
  const pts = points.filter(p => Number.isFinite(p?.tempC) && Number.isFinite(p?.pressureHpa))
    .map(p => `${xFn(p.tempC, p.pressureHpa).toFixed(1)},${yFn(p.pressureHpa).toFixed(1)}`);
  return pts.length >= 2 ? `M ${pts.join(' L ')}` : '';
}

function _dryAdiabat(thetaC, pBot, pTop, step = 25) {
  const pts = [];
  for (let p = pBot; p >= pTop; p -= step) {
    pts.push({ pressureHpa: p, tempC: ((thetaC + _Z) / Math.pow(1000 / p, _ROCP)) - _Z });
  }
  return pts;
}

function _moistAdiabat(tC, pBot, pTop, step = 10) {
  const pts = [];
  for (let p = pBot; p >= pTop; p -= step) {
    pts.push({ pressureHpa: p, tempC: _wetlift(1000, tC, p) });
  }
  return pts;
}

function _mixingRatio(w, pBot, pTop, step = 25) {
  const pts = [];
  for (let p = pBot; p >= pTop; p -= step) {
    const t = _tempAtMixrat(w, p);
    if (Number.isFinite(t)) pts.push({ pressureHpa: p, tempC: t });
  }
  return pts;
}

function _interpolateTemp(points, pHpa) {
  if (!Array.isArray(points) || points.length < 2 || !Number.isFinite(pHpa)) return null;
  for (let i = 0; i < points.length - 1; i++) {
    const a = points[i], b = points[i + 1];
    if (!Number.isFinite(a?.pressure_hpa) || !Number.isFinite(b?.pressure_hpa) || !Number.isFinite(a?.temp_c) || !Number.isFinite(b?.temp_c)) continue;
    const top = Math.min(a.pressure_hpa, b.pressure_hpa);
    const bot = Math.max(a.pressure_hpa, b.pressure_hpa);
    if (pHpa < top || pHpa > bot) continue;
    const d = Math.log(b.pressure_hpa) - Math.log(a.pressure_hpa);
    if (Math.abs(d) < 1e-6) return a.temp_c;
    return a.temp_c + ((b.temp_c - a.temp_c) * ((Math.log(pHpa) - Math.log(a.pressure_hpa)) / d));
  }
  return null;
}

function _parcelShade(parcel, env, pTop, pBot, xFn, yFn) {
  const r = { cape: [], cin: [] };
  if (!Array.isArray(parcel) || parcel.length < 2 || !Array.isArray(env) || env.length < 2) return r;
  const segs = [];
  for (let i = 0; i < parcel.length - 1; i++) {
    const a = parcel[i], b = parcel[i + 1];
    if (!Number.isFinite(a?.pressure_hpa) || !Number.isFinite(b?.pressure_hpa)) continue;
    if (a.pressure_hpa > pBot || a.pressure_hpa < pTop || b.pressure_hpa > pBot || b.pressure_hpa < pTop) continue;
    const eA = _interpolateTemp(env, a.pressure_hpa), eB = _interpolateTemp(env, b.pressure_hpa);
    if (!Number.isFinite(eA) || !Number.isFinite(eB)) continue;
    segs.push({ a: { p: a.pressure_hpa, pt: a.temp_c, et: eA }, b: { p: b.pressure_hpa, pt: b.temp_c, et: eB } });
  }
  const poly = (bucket, pts) => {
    if (!pts || pts.length < 3) return;
    const c = pts.map(p => `${xFn(p.t, p.p).toFixed(1)},${yFn(p.p).toFixed(1)}`);
    if (c.length >= 3) r[bucket].push(`M ${c.join(' L ')} Z`);
  };
  for (const s of segs) {
    const dA = s.a.pt - s.a.et, dB = s.b.pt - s.b.et;
    const same = (dA >= 0 && dB >= 0) || (dA <= 0 && dB <= 0);
    const bk = d => d >= 0 ? 'cape' : 'cin';
    if (same) { poly(bk(dA), [{ p: s.a.p, t: s.a.pt }, { p: s.b.p, t: s.b.pt }, { p: s.b.p, t: s.b.et }, { p: s.a.p, t: s.a.et }]); continue; }
    const f = dA / (dA - dB);
    if (!Number.isFinite(f)) continue;
    const cp = s.a.p + ((s.b.p - s.a.p) * f);
    const ct = s.a.pt + ((s.b.pt - s.a.pt) * f);
    poly(bk(dA), [{ p: s.a.p, t: s.a.pt }, { p: cp, t: ct }, { p: cp, t: ct }, { p: s.a.p, t: s.a.et }]);
    poly(bk(dB), [{ p: cp, t: ct }, { p: s.b.p, t: s.b.pt }, { p: s.b.p, t: s.b.et }, { p: cp, t: ct }]);
  }
  return r;
}

function _normTop(min) {
  if (!Number.isFinite(min)) return 200;
  if (min <= 100) return 100; if (min <= 150) return 150; if (min <= 200) return 200;
  if (min <= 250) return 250; if (min <= 300) return 300; if (min <= 400) return 400;
  return 500;
}

function _pressureLevels(top) {
  const l = []; for (let p = 1000; p >= top; p -= 100) l.push(p); return l;
}

// ── Wind barb builder ────────────────────────────────────────

function _windBarb(x, y, dir, spd, len = 16) {
  if (!Number.isFinite(dir) || !Number.isFinite(spd)) return '';
  const a = (dir * Math.PI) / 180;
  const sin = Math.sin(a), cos = Math.cos(a);
  const xt = x + sin * len, yt = y + cos * len;
  const p = [`<line class="wind-staff" x1="${x.toFixed(1)}" y1="${y.toFixed(1)}" x2="${xt.toFixed(1)}" y2="${yt.toFixed(1)}"></line>`];
  let rem = Math.max(0, Math.round(spd / 5) * 5);
  let full = Math.floor((rem % 50) / 10);
  const half = (rem % 10) >= 5;
  const pen = Math.floor(rem / 50);
  const nx = sin, ny = cos, px = cos, py = -sin;
  let off = 0; const sp = 3.6, fl = 7, hl = 4;
  for (let i = 0; i < pen; i++) {
    const bx = xt - nx * off, by = yt - ny * off;
    const no = off + sp, nxp = xt - nx * no, nyp = yt - ny * no;
    const wx = bx + px * fl, wy = by + py * fl;
    p.push(`<polygon class="wind-pennant" points="${bx.toFixed(1)},${by.toFixed(1)} ${nxp.toFixed(1)},${nyp.toFixed(1)} ${wx.toFixed(1)},${wy.toFixed(1)}"></polygon>`);
    off += sp + 1.2;
  }
  for (let i = 0; i < full; i++) {
    const bx = xt - nx * off, by = yt - ny * off;
    p.push(`<line class="wind-staff" x1="${bx.toFixed(1)}" y1="${by.toFixed(1)}" x2="${(bx + px * fl).toFixed(1)}" y2="${(by + py * fl).toFixed(1)}"></line>`);
    off += sp;
  }
  if (half) {
    const bx = xt - nx * off, by = yt - ny * off;
    p.push(`<line class="wind-staff" x1="${bx.toFixed(1)}" y1="${by.toFixed(1)}" x2="${(bx + px * hl).toFixed(1)}" y2="${(by + py * hl).toFixed(1)}"></line>`);
  }
  return p.join('');
}

// ── Skew-T chart ─────────────────────────────────────────────

function renderMobileSkewT(container, data) {
  if (!container) return;
  const levels = Array.isArray(data?.levels) ? data.levels.filter(r => Number.isFinite(r.pressure_hpa)) : [];
  const plotData = data?.diagnostics?.plot_data || {};
  const wetbulb = Array.isArray(plotData.wetbulb_profile) ? plotData.wetbulb_profile.filter(r => Number.isFinite(r?.pressure_hpa) && Number.isFinite(r?.temp_c)) : [];
  const parcel = Array.isArray(plotData.surface_parcel) ? plotData.surface_parcel.filter(r => Number.isFinite(r?.pressure_hpa) && Number.isFinite(r?.temp_c)) : [];
  if (!levels.length) { container.innerHTML = ''; return; }

  const W = 404, H = 452;
  const m = { top: 18, right: 74, bottom: 34, left: 38 };
  const pW = W - m.left - m.right, pH = H - m.top - m.bottom;
  const pBot = 1000;
  const allP = levels.map(r => r.pressure_hpa).concat(wetbulb.map(r => r.pressure_hpa)).concat(parcel.map(r => r.pressure_hpa)).filter(Number.isFinite);
  const pTop = _normTop(Math.min(...allP));
  const skew = 56, tMin = -50, tMax = 50;
  const windX = W - 28, yBot = m.top + pH;

  const pToY = p => {
    const tl = Math.log(pTop), bl = Math.log(pBot), vl = Math.log(Math.max(pTop, Math.min(pBot, p)));
    return m.top + ((vl - tl) / (bl - tl)) * pH;
  };
  const tToX = (t, p) => {
    const bx = m.left + ((t - tMin) / (tMax - tMin)) * pW;
    return bx + ((yBot - pToY(p)) / pH) * skew;
  };

  const svg = [];
  svg.push(`<rect x="0" y="0" width="${W}" height="${H}" rx="8" fill="transparent"></rect>`);
  svg.push(`<defs><clipPath id="mSkClip"><rect x="${m.left}" y="${m.top}" width="${pW}" height="${pH}"></rect></clipPath></defs>`);

  // Isobars
  for (const p of _pressureLevels(pTop)) {
    const y = pToY(p);
    svg.push(`<line class="grid-line major" x1="${m.left}" y1="${y.toFixed(1)}" x2="${m.left + pW}" y2="${y.toFixed(1)}"></line>`);
    svg.push(`<text class="level-label" x="${m.left - 5}" y="${(y + 3).toFixed(1)}" text-anchor="end">${p}</text>`);
  }

  svg.push(`<g clip-path="url(#mSkClip)">`);

  // Isotherms
  const isos = []; for (let t = -40; t <= 40; t += 10) isos.push(t);
  for (const t of isos) {
    svg.push(`<line class="grid-line iso-therm" x1="${tToX(t, pBot).toFixed(1)}" y1="${yBot.toFixed(1)}" x2="${tToX(t, pTop).toFixed(1)}" y2="${m.top.toFixed(1)}"></line>`);
  }

  // Dry adiabats
  for (let t = -20; t <= 200; t += 40) {
    const d = _buildThermoPath(_dryAdiabat(t, pBot, pTop), tToX, pToY);
    if (d) svg.push(`<path class="dry-adiabat-line" d="${d}"></path>`);
  }
  // Moist adiabats
  for (const t of [-10, 10, 30]) {
    const d = _buildThermoPath(_moistAdiabat(t, pBot, pTop), tToX, pToY);
    if (d) svg.push(`<path class="moist-adiabat-line" d="${d}"></path>`);
  }
  // Mixing ratios
  for (const w of [4, 8, 12, 16]) {
    const d = _buildThermoPath(_mixingRatio(w, pBot, 600), tToX, pToY);
    if (d) svg.push(`<path class="mixing-ratio-line" d="${d}"></path>`);
  }

  // Parcel shading
  const envTrace = levels.filter(l => Number.isFinite(l.temp_c)).map(l => ({ pressure_hpa: l.pressure_hpa, temp_c: l.temp_c })).sort((a, b) => b.pressure_hpa - a.pressure_hpa);
  const shade = _parcelShade(parcel, envTrace, pTop, pBot, tToX, pToY);
  shade.cin.forEach(d => svg.push(`<path class="cin-fill" d="${d}"></path>`));
  shade.cape.forEach(d => svg.push(`<path class="cape-fill" d="${d}"></path>`));

  // Profiles
  const tPath = _buildPath(levels, l => l.temp_c, tToX, pToY);
  const dPath = _buildPath(levels, l => l.dewpoint_c, tToX, pToY);
  const wPath = _buildPath(wetbulb, l => l.temp_c, tToX, pToY);
  const pPath = _buildPath(parcel, l => l.temp_c, tToX, pToY);
  if (dPath) svg.push(`<path class="dewpoint-line" d="${dPath}"></path>`);
  if (wPath) svg.push(`<path class="wetbulb-line" d="${wPath}"></path>`);
  if (tPath) svg.push(`<path class="temp-line" d="${tPath}"></path>`);
  if (pPath) svg.push(`<path class="parcel-line" d="${pPath}"></path>`);
  svg.push(`</g>`);

  // Isotherm labels
  for (const t of isos) {
    svg.push(`<text class="axis-label" x="${tToX(t, pBot).toFixed(1)}" y="${(yBot + 13).toFixed(1)}" text-anchor="middle">${t}</text>`);
  }
  svg.push(`<text class="axis-label" x="${m.left}" y="${(m.top - 5).toFixed(1)}">mb</text>`);
  svg.push(`<text class="axis-label" x="${windX - 4}" y="${(m.top - 5).toFixed(1)}" text-anchor="end">wind</text>`);

  // Wind barbs
  for (const l of levels) {
    if (!Number.isFinite(l.wind_speed_kt) || !Number.isFinite(l.wind_dir_deg)) continue;
    const y = pToY(l.pressure_hpa);
    svg.push(_windBarb(windX, y, l.wind_dir_deg, l.wind_speed_kt));
  }

  container.innerHTML = `
    <div class="sounding-card">
      <div class="sounding-card-title">Skew-T / Parcel</div>
      <svg viewBox="0 0 ${W} ${H}" class="sounding-chart">${svg.join('')}</svg>
    </div>`;
}

// ── Hodograph ────────────────────────────────────────────────

function renderMobileHodograph(container, data) {
  if (!container) return;
  const levels = Array.isArray(data?.levels) ? data.levels : [];
  const sw = data?.surface?.wind || null;
  const diag = data?.diagnostics || {};
  const pts = [];
  if (sw && Number.isFinite(sw.u_kt) && Number.isFinite(sw.v_kt)) pts.push({ h: 0, u: sw.u_kt, v: sw.v_kt });
  for (const l of levels) {
    if (!Number.isFinite(l?.u_kt) || !Number.isFinite(l?.v_kt) || !Number.isFinite(l?.height_m)) continue;
    pts.push({ h: l.height_m, u: l.u_kt, v: l.v_kt });
  }
  pts.sort((a, b) => a.h - b.h);
  if (pts.length < 2) { container.innerHTML = ''; return; }

  const motions = [diag.bunkers_right, diag.bunkers_left].filter(w => w && Number.isFinite(w.u_kt) && Number.isFinite(w.v_kt));
  const maxC = pts.concat(motions).reduce((m, p) => Math.max(m, Math.abs(p.u_kt || p.u || 0), Math.abs(p.v_kt || p.v || 0)), 40);
  const rMax = Math.max(40, Math.ceil(maxC / 20) * 20);
  const S = 244, cx = S / 2, cy = S / 2, pr = 94;
  const sc = pr / rMax;
  const proj = (u, v) => ({ x: cx + u * sc, y: cy - v * sc });

  const svg = [];
  for (let s = 20; s <= rMax; s += 20) {
    const r = s * sc;
    svg.push(`<circle class="grid-circle${s === rMax ? ' major' : ''}" cx="${cx}" cy="${cy}" r="${r.toFixed(1)}"></circle>`);
    svg.push(`<text class="ring-label" x="${cx + r + 3}" y="${cy - 3}">${s}</text>`);
  }
  svg.push(`<line class="grid-axis" x1="${cx}" y1="${cy - pr}" x2="${cx}" y2="${cy + pr}"></line>`);
  svg.push(`<line class="grid-axis" x1="${cx - pr}" y1="${cy}" x2="${cx + pr}" y2="${cy}"></line>`);

  const path = pts.map(p => { const q = proj(p.u, p.v); return `${q.x.toFixed(1)},${q.y.toFixed(1)}`; }).join(' L ');
  svg.push(`<path class="hodo-line" d="M ${path}"></path>`);

  for (const p of pts) {
    const q = proj(p.u, p.v);
    svg.push(`<circle class="hodo-point" cx="${q.x.toFixed(1)}" cy="${q.y.toFixed(1)}" r="2.5"></circle>`);
  }
  const labels = [{ t: 0, l: 'SFC' }, { t: 1000, l: '1km' }, { t: 3000, l: '3km' }, { t: 6000, l: '6km' }];
  for (const tgt of labels) {
    let best = null, bd = Infinity;
    for (const p of pts) { const d = Math.abs(p.h - tgt.t); if (d < bd) { bd = d; best = p; } }
    if (!best || bd > 1750) continue;
    const q = proj(best.u, best.v);
    svg.push(`<text class="height-label" x="${(q.x + 5).toFixed(1)}" y="${(q.y - 5).toFixed(1)}">${tgt.l}</text>`);
  }

  if (diag.bunkers_right && Number.isFinite(diag.bunkers_right.u_kt)) {
    const q = proj(diag.bunkers_right.u_kt, diag.bunkers_right.v_kt);
    svg.push(`<circle class="storm-motion" cx="${q.x.toFixed(1)}" cy="${q.y.toFixed(1)}" r="3.5"></circle>`);
    svg.push(`<text class="height-label" x="${(q.x + 6).toFixed(1)}" y="${(q.y - 6).toFixed(1)}">RM</text>`);
  }
  if (diag.bunkers_left && Number.isFinite(diag.bunkers_left.u_kt)) {
    const q = proj(diag.bunkers_left.u_kt, diag.bunkers_left.v_kt);
    svg.push(`<circle class="storm-motion" cx="${q.x.toFixed(1)}" cy="${q.y.toFixed(1)}" r="3.5"></circle>`);
    svg.push(`<text class="height-label" x="${(q.x + 6).toFixed(1)}" y="${(q.y - 6).toFixed(1)}">LM</text>`);
  }

  container.innerHTML = `
    <div class="sounding-card">
      <div class="sounding-card-title">Hodograph</div>
      <svg viewBox="0 0 ${S} ${S}" class="sounding-hodo">${svg.join('')}</svg>
    </div>`;
}

// ── Diagnostics panel ────────────────────────────────────────

function renderMobileSoundingDiag(container, data) {
  if (!container) return;
  const d = data.diagnostics || {};
  const s = data.surface || {};
  const w = s.wind || {};
  const fmt = (v, u = '') => Number.isFinite(v) ? `${Math.round(v)}${u}` : '--';
  const fmt1 = (v, u = '') => Number.isFinite(v) ? `${v.toFixed(1)}${u}` : '--';
  const fmt2 = (v) => Number.isFinite(v) ? v.toFixed(2) : '--';

  const rows = [
    ['Watch Type', d.watch_type || 'NONE'],
    ['MLCAPE', fmt(d.mlcape_jkg, ' J/kg')],
    ['MLCIN', fmt(d.mlcin_jkg, ' J/kg')],
    ['MUCAPE', fmt(d.mucape_jkg, ' J/kg')],
    ['LCL', fmt(d.mllcl_m, ' m')],
    ['0-1km SRH', fmt(d.srh_1km_m2s2, ' m\u00B2/s\u00B2')],
    ['0-3km SRH', fmt(d.srh_3km_m2s2, ' m\u00B2/s\u00B2')],
    ['EBWD', fmt(d.ebwd_kt, ' kt')],
    ['STP (CIN)', fmt2(d.stp_cin)],
    ['SCP', fmt2(d.scp)],
    ['SHIP', fmt2(d.ship)],
    ['Sfc Wind', Number.isFinite(w.dir_deg) ? `${Math.round(w.dir_deg)}/${Math.round(w.speed_kt)} kt` : '--'],
    ['Sfc Temp', fmt1(s.temp_c, ' C')],
    ['Sfc Dewp', fmt1(s.dewpoint_c, ' C')],
    ['LI (500)', fmt1(d.li5_c, ' C')],
    ['DCAPE', fmt(d.dcape_jkg, ' J/kg')],
  ];

  container.innerHTML = `
    <div class="sounding-card">
      <div class="sounding-card-title">Diagnostics</div>
      <div class="m-snd-diag">${rows.map(([l, v]) =>
        `<div class="m-snd-row"><span class="m-snd-label">${l}</span><span class="m-snd-val">${v}</span></div>`
      ).join('')}</div>
    </div>`;
}
