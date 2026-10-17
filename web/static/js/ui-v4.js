'use strict';

/**
 * WX Viewer — v4 interface pieces (only when <html data-ui="v4">).
 *
 * Adds three progressive layers over the existing controls without
 * replacing them, so app.js keeps owning model/run state:
 *  - model chips that drive #modelSelect (plus a "More" menu for the rest)
 *  - a run card wrapped around #runSelect (the native select stays usable)
 *  - a point readout panel fed by app.js's `wx:point-query` event
 */
(function initUiV4() {
  if (document.documentElement.dataset.ui !== 'v4') return;

  const PRIMARY_MODELS = [
    ['hrrr', 'HRRR'], ['nam3km', 'NAM 3km'], ['rap', 'RAP'], ['gfs', 'GFS'], ['ecmwf', 'ECMWF'],
  ];
  const METEOGRAM_MODELS = new Set(['hrrr', 'rrfs', 'nam3km', 'rap', 'nam12km', 'href', 'gfs', 'gefs']);
  const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

  const modelSelect = document.getElementById('modelSelect');
  const runSelect = document.getElementById('runSelect');
  const mapSection = document.querySelector('.map-section');

  /** Build an element: el('div', {class: 'x'}, [children or text]). */
  function el(tag, attrs = {}, children = []) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value === false || value == null) continue;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = value;
      else node.setAttribute(key, value === true ? '' : value);
    }
    for (const child of [].concat(children)) {
      if (child) node.append(child);
    }
    return node;
  }

  function svgIcon(path, size = 14) {
    const wrap = document.createElement('span');
    wrap.className = 'v4-icon';
    wrap.setAttribute('aria-hidden', 'true');
    wrap.innerHTML = `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${path}</svg>`;
    return wrap;
  }

  /* ── Model chips ─────────────────────────────────────────── */

  function chooseModel(value) {
    if (!modelSelect || modelSelect.value === value) return;
    modelSelect.value = value;
    modelSelect.dispatchEvent(new Event('change', { bubbles: true }));
    syncModelChips();
  }

  let chipRow = null;
  let moreBtn = null;
  let moreMenu = null;

  function closeMoreMenu() {
    if (!moreMenu || moreMenu.hidden) return;
    moreMenu.hidden = true;
    moreBtn.setAttribute('aria-expanded', 'false');
  }

  function openMoreMenu() {
    moreMenu.replaceChildren(...[...modelSelect.options].map((opt) => {
      const item = el('button', {
        type: 'button',
        class: 'v4-menu-item',
        role: 'menuitemradio',
        'aria-checked': opt.value === modelSelect.value ? 'true' : 'false',
        text: opt.textContent.trim(),
      });
      item.addEventListener('click', function onMoreItemClick() {
        closeMoreMenu();
        chooseModel(opt.value);
        moreBtn.focus();
      });
      return item;
    }));
    moreMenu.hidden = false;
    moreBtn.setAttribute('aria-expanded', 'true');
    moreMenu.querySelector('[aria-checked="true"]')?.focus() || moreMenu.firstElementChild?.focus();
  }

  function buildModelChips() {
    if (!modelSelect) return;
    const group = modelSelect.closest('.sidebar-control-group');
    chipRow = el('div', { class: 'v4-chip-row', role: 'group', 'aria-label': 'Model' });
    for (const [value, label] of PRIMARY_MODELS) {
      if (![...modelSelect.options].some((opt) => opt.value === value)) continue;
      const chip = el('button', { type: 'button', class: 'v4-chip', 'data-model': value, 'aria-pressed': 'false', text: label });
      chip.addEventListener('click', function onModelChipClick() { chooseModel(value); });
      chipRow.append(chip);
    }
    moreBtn = el('button', {
      type: 'button', class: 'v4-chip v4-chip--more', 'aria-haspopup': 'menu', 'aria-expanded': 'false',
    }, [el('span', { class: 'v4-chip-label', text: 'More' }), svgIcon('<path d="m6 9 6 6 6-6"/>', 12)]);
    moreMenu = el('div', { class: 'v4-menu', role: 'menu', 'aria-label': 'All models' });
    moreMenu.hidden = true;
    moreBtn.addEventListener('click', function onMoreClick() {
      if (moreMenu.hidden) openMoreMenu(); else closeMoreMenu();
    });
    moreMenu.addEventListener('keydown', function onMoreMenuKey(evt) {
      const items = [...moreMenu.querySelectorAll('.v4-menu-item')];
      const idx = items.indexOf(document.activeElement);
      if (evt.key === 'Escape') { closeMoreMenu(); moreBtn.focus(); evt.preventDefault(); }
      else if (evt.key === 'ArrowDown') { items[(idx + 1) % items.length]?.focus(); evt.preventDefault(); }
      else if (evt.key === 'ArrowUp') { items[(idx - 1 + items.length) % items.length]?.focus(); evt.preventDefault(); }
    });
    document.addEventListener('click', function onDocClickForMenu(evt) {
      if (!moreMenu.hidden && !moreMenu.contains(evt.target) && !moreBtn.contains(evt.target)) closeMoreMenu();
    });
    chipRow.append(el('div', { class: 'v4-more-wrap' }, [moreBtn, moreMenu]));
    group.classList.add('v4-has-chips');
    group.append(chipRow);
  }

  function syncModelChips() {
    if (!chipRow) return;
    const current = modelSelect.value;
    let matched = false;
    for (const chip of chipRow.querySelectorAll('.v4-chip[data-model]')) {
      const on = chip.dataset.model === current;
      matched ||= on;
      chip.classList.toggle('active', on);
      chip.setAttribute('aria-pressed', on ? 'true' : 'false');
    }
    // A model outside the chip set shows its name on the "More" chip.
    const label = moreBtn.querySelector('.v4-chip-label');
    label.textContent = matched ? 'More' : (modelSelect.selectedOptions[0]?.textContent.trim() || 'More');
    moreBtn.classList.toggle('active', !matched);
  }

  /* ── Run card ────────────────────────────────────────────── */

  let runCard = null;

  function buildRunCard() {
    if (!runSelect) return;
    const group = runSelect.closest('.sidebar-control-group');
    runCard = el('div', { class: 'v4-run-card' }, [
      el('span', { class: 'v4-run-dot', 'aria-hidden': 'true' }),
      el('span', { class: 'v4-run-text' }, [
        el('span', { class: 'v4-run-title', text: '--' }),
        el('span', { class: 'v4-run-meta', text: '' }),
      ]),
      svgIcon('<path d="m6 9 6 6 6-6"/>'),
    ]);
    // The real select sits invisibly over the card so the native picker opens.
    runSelect.parentNode.insertBefore(runCard, runSelect);
    runCard.append(runSelect);
    group.classList.add('v4-has-run-card');
  }

  function syncRunCard() {
    if (!runCard) return;
    const runId = runSelect.value || '';
    const option = runSelect.selectedOptions[0];
    const status = option ? (option.textContent.split('·')[1] || '').trim() : '';
    const title = runCard.querySelector('.v4-run-title');
    const meta = runCard.querySelector('.v4-run-meta');
    if (/^\d{10}$/.test(runId)) {
      const d = new Date(Date.UTC(+runId.slice(0, 4), +runId.slice(4, 6) - 1, +runId.slice(6, 8), +runId.slice(8, 10)));
      title.textContent = `${runId.slice(8, 10)}Z run · ${DAYS[d.getUTCDay()]} ${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}`;
    } else {
      title.textContent = option?.textContent.trim() || 'No runs';
    }
    const latest = runSelect.selectedIndex === 0 && runSelect.options.length > 1;
    meta.textContent = [status, latest ? 'latest run' : ''].filter(Boolean).join(' · ');
    const tone = /complete/i.test(status) ? 'ok' : (/old|stale/i.test(status) ? 'stale' : 'busy');
    runCard.dataset.tone = runId ? tone : 'none';
  }

  /* ── Point readout ───────────────────────────────────────── */

  let readout = null;
  let readoutToken = 0;
  let lastPoint = null;

  function formatLatLon(lat, lng) {
    const ns = lat >= 0 ? 'N' : 'S';
    const ew = lng >= 0 ? 'E' : 'W';
    return `${Math.abs(lat).toFixed(2)}°${ns} ${Math.abs(lng).toFixed(2)}°${ew}`;
  }

  function buildReadout() {
    if (!mapSection) return;
    const closeBtn = el('button', { type: 'button', class: 'v4-icon-btn', 'aria-label': 'Close point readout' },
      [svgIcon('<path d="M18 6 6 18M6 6l12 12"/>')]);
    const forecastBtn = el('button', { type: 'button', class: 'v4-btn v4-btn--primary', text: 'Model plume' });
    const soundingBtn = el('button', { type: 'button', class: 'v4-btn', text: 'Sounding' });
    readout = el('section', { class: 'v4-readout', 'aria-label': 'Point readout', 'aria-live': 'polite' }, [
      el('div', { class: 'v4-readout-head' }, [
        el('div', {}, [
          el('div', { class: 'v4-readout-title', text: 'Selected point' }),
          el('div', { class: 'v4-readout-coords', text: '' }),
        ]),
        closeBtn,
      ]),
      el('div', { class: 'v4-readout-main' }, [
        el('span', { class: 'v4-readout-value', text: '--' }),
        el('span', { class: 'v4-readout-label', text: '' }),
      ]),
      el('div', { class: 'v4-readout-stats' }),
      el('figure', { class: 'v4-readout-trend' }, [
        el('figcaption', { class: 'v4-readout-caption', text: '' }),
        el('div', { class: 'v4-readout-spark' }),
        el('div', { class: 'v4-readout-axis' }),
      ]),
      el('div', { class: 'v4-readout-actions' }, [forecastBtn, soundingBtn]),
    ]);
    readout.hidden = true;
    closeBtn.addEventListener('click', function onReadoutClose() { hideReadout(); });
    forecastBtn.addEventListener('click', function onReadoutForecast() {
      if (!lastPoint || !window.WXMeteogram?.open) return;
      const point = lastPoint;
      hideReadout();
      window.WXMeteogram.open(point);
    });
    soundingBtn.addEventListener('click', function onReadoutSounding() {
      if (!lastPoint || typeof window.loadModelSounding !== 'function') return;
      const point = lastPoint;
      hideReadout();
      window.loadModelSounding(point);
    });
    mapSection.append(readout);
  }

  function hideReadout() {
    if (!readout) return;
    readout.hidden = true;
    readoutToken += 1;
    document.documentElement.classList.remove('v4-readout-open');
  }

  function statTile(label, value) {
    return el('div', { class: 'v4-stat' }, [
      el('span', { class: 'v4-stat-label', text: label }),
      el('span', { class: 'v4-stat-value', text: value }),
    ]);
  }

  /** Inline SVG sparkline of [fxx, value] pairs with a marker at `fxx`. */
  function sparkline(points, fxx) {
    const W = 262; const H = 60; const pad = 4;
    const xs = points.map((p) => p[0]); const ys = points.map((p) => p[1]);
    const x0 = Math.min(...xs); const x1 = Math.max(...xs);
    const y0 = Math.min(...ys); const y1 = Math.max(...ys);
    const sx = (x) => pad + ((x - x0) / Math.max(1, x1 - x0)) * (W - pad * 2);
    const sy = (y) => H - pad - ((y - y0) / Math.max(1, y1 - y0)) * (H - pad * 2);
    const line = points.map((p) => `${sx(p[0]).toFixed(1)},${sy(p[1]).toFixed(1)}`).join(' ');
    const here = points.reduce((best, p) => (Math.abs(p[0] - fxx) < Math.abs(best[0] - fxx) ? p : best), points[0]);
    const lo = Math.round(y0); const hi = Math.round(y1);
    return `<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img" aria-label="2 m temperature ranges from ${lo} to ${hi} degrees over this run">`
      + `<line x1="0" y1="${H - 1}" x2="${W}" y2="${H - 1}" stroke="rgba(255,255,255,0.08)"/>`
      + `<polyline points="${line}" fill="none" stroke="#6cb6ff" stroke-width="2" stroke-linejoin="round"/>`
      + `<line x1="${sx(here[0]).toFixed(1)}" y1="0" x2="${sx(here[0]).toFixed(1)}" y2="${H}" stroke="rgba(255,255,255,0.35)" stroke-dasharray="3 3"/>`
      + `<circle cx="${sx(here[0]).toFixed(1)}" cy="${sy(here[1]).toFixed(1)}" r="4" fill="#0f1216" stroke="#eceff3" stroke-width="2"/>`
      + '</svg>';
  }

  function valueAt(series, fxx) {
    if (!Array.isArray(series) || !series.length) return null;
    const hit = series.reduce((best, p) => (Math.abs(p[0] - fxx) < Math.abs(best[0] - fxx) ? p : best), series[0]);
    return Number.isFinite(hit[1]) ? hit[1] : null;
  }

  function localHourLabel(initIso, fxx) {
    const d = new Date(Date.parse(initIso) + fxx * 3600e3);
    const h = d.getHours();
    return `${h % 12 || 12}${h < 12 ? 'a' : 'p'}`;
  }

  async function showReadout(detail) {
    if (!readout) return;
    const appState = window.state || {};
    lastPoint = { lat: detail.lat, lng: detail.lng };
    const token = ++readoutToken;

    readout.querySelector('.v4-readout-coords').textContent = formatLatLon(detail.lat, detail.lng);
    readout.querySelector('.v4-readout-value').textContent = detail.value || '--';
    readout.querySelector('.v4-readout-label').textContent = detail.label || '';
    const stats = readout.querySelector('.v4-readout-stats');
    const trend = readout.querySelector('.v4-readout-trend');
    stats.replaceChildren();
    trend.hidden = true;
    readout.hidden = false;
    document.documentElement.classList.add('v4-readout-open');

    const model = METEOGRAM_MODELS.has(appState.model) ? appState.model : 'hrrr';
    const sameRun = model === appState.model && /^\d{10}$/.test(appState.runId || '');
    const params = new URLSearchParams({ lat: detail.lat.toFixed(4), lon: detail.lng.toFixed(4) });
    if (sameRun) params.set('run_id', appState.runId);
    stats.append(el('div', { class: 'v4-readout-loading', text: 'Loading model plume…' }));
    let data = null;
    try {
      const resp = await fetch(`/api/meteogram/${encodeURIComponent(model)}?${params}`);
      if (resp.ok) data = await resp.json();
    } catch (err) {
      data = null;
    }
    if (token !== readoutToken) return;
    stats.replaceChildren();
    if (!data?.series) {
      stats.append(el('div', { class: 'v4-readout-loading', text: 'No model plume for this location.' }));
      return;
    }

    const fxx = sameRun ? (appState.currentFxx ?? 0) : 0;
    const u = data.units || {};
    const temp = valueAt(data.series.temp_2m, fxx);
    const dew = valueAt(data.series.dewpoint_2m, fxx);
    const gust = valueAt(data.series.wind_gusts, fxx);
    const fmt = (v, unit) => (v == null ? '--' : `${Math.round(v)}${unit === '°F' ? '°' : ` ${unit || ''}`}`.trim());
    stats.append(
      statTile('Temp', fmt(temp, u.temp_2m)),
      statTile('Dewpoint', fmt(dew, u.dewpoint_2m)),
      statTile('Gust', fmt(gust, u.wind_gusts)),
    );

    const series = (data.series.temp_2m || []).filter((p) => Number.isFinite(p[1]));
    if (series.length > 2) {
      trend.querySelector('.v4-readout-caption').textContent = `${data.label || model.toUpperCase()} 2 m temperature · ${data.run_id.slice(8, 10)}Z run`;
      trend.querySelector('.v4-readout-spark').innerHTML = sparkline(series, fxx);
      const axis = trend.querySelector('.v4-readout-axis');
      const first = series[0][0]; const last = series[series.length - 1][0];
      const ticks = [0, 1, 2, 3].map((i) => Math.round(first + ((last - first) * i) / 3));
      axis.replaceChildren(...ticks.map((t) => el('span', { text: data.init_time ? localHourLabel(data.init_time, t) : `F${t}` })));
      trend.hidden = false;
    }
  }

  document.addEventListener('wx:point-query', function onPointQuery(evt) {
    if (evt.detail?.source !== 'models') return;
    showReadout(evt.detail);
  });

  // Leaving the models view closes the readout.
  for (const tab of document.querySelectorAll('.source-tab')) {
    tab.addEventListener('click', function onSourceTabClick() {
      if (tab.dataset.source !== 'models') hideReadout();
    });
  }
  document.addEventListener('keydown', function onReadoutEscape(evt) {
    // Only when focus is in the readout, so Escape on other panels leaves it be.
    if (evt.key === 'Escape' && readout && !readout.hidden && readout.contains(document.activeElement)) hideReadout();
  });

  /* ── Radar alerts panel ──────────────────────────────────── */

  const ALERT_REFRESH_MS = 60 * 1000;
  let alertsPanel = null;
  let alertsData = null;
  let alertsTab = 'warnings';
  let alertsTimer = null;

  function isWarning(event) {
    return /warning|emergency/i.test(event || '');
  }

  function timeLeft(expiresIso) {
    const ms = Date.parse(expiresIso) - Date.now();
    if (!Number.isFinite(ms)) return '';
    if (ms <= 0) return 'expiring';
    const mins = Math.round(ms / 60000);
    if (mins < 60) return `${mins} min`;
    const h = Math.floor(mins / 60);
    return `${h} h ${String(mins % 60).padStart(2, '0')}`;
  }

  function relativeAge(iso) {
    const mins = Math.round((Date.now() - Date.parse(iso)) / 60000);
    if (!Number.isFinite(mins) || mins < 1) return 'just now';
    return mins < 60 ? `${mins} min ago` : `${Math.round(mins / 60)} h ago`;
  }

  /** Zoom the map to a GeoJSON feature using app.js's Leaflet map. */
  function zoomToFeature(feature) {
    // eslint-disable-next-line no-undef
    const map = typeof leafletMap !== 'undefined' ? leafletMap : null;
    if (!map || !feature?.geometry || !window.L) return;
    const bounds = window.L.geoJSON(feature).getBounds();
    if (bounds.isValid()) map.flyToBounds(bounds, { padding: [80, 80], maxZoom: 9, duration: 0.8 });
  }

  function buildAlertsPanel() {
    if (!mapSection) return;
    const tabs = el('div', { class: 'v4-segmented', role: 'tablist', 'aria-label': 'Alert type' });
    for (const [key, label] of [['warnings', 'Warnings'], ['other', 'Advisories']]) {
      const tab = el('button', { type: 'button', role: 'tab', class: 'v4-seg', 'data-tab': key, 'aria-selected': key === alertsTab ? 'true' : 'false' },
        [el('span', { text: label }), el('span', { class: 'v4-seg-count', text: '' })]);
      tab.addEventListener('click', function onAlertsTabClick() {
        alertsTab = key;
        renderAlerts();
      });
      tabs.append(tab);
    }
    const collapseBtn = el('button', { type: 'button', class: 'v4-icon-btn', 'aria-label': 'Hide alerts', 'aria-expanded': 'true' },
      [svgIcon('<path d="m18 15-6-6-6 6"/>')]);
    alertsPanel = el('section', { class: 'v4-alerts', 'aria-label': 'Active alerts' }, [
      el('div', { class: 'v4-alerts-head' }, [
        el('div', {}, [
          el('div', { class: 'v4-readout-title', text: 'Active alerts' }),
          el('div', { class: 'v4-alerts-updated', text: 'Loading…' }),
        ]),
        collapseBtn,
      ]),
      el('div', { class: 'v4-alerts-body' }, [tabs, el('ul', { class: 'v4-alerts-list', role: 'list' })]),
    ]);
    collapseBtn.addEventListener('click', function onAlertsCollapse() {
      const collapsed = alertsPanel.classList.toggle('collapsed');
      collapseBtn.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
      collapseBtn.setAttribute('aria-label', collapsed ? 'Show alerts' : 'Hide alerts');
    });
    alertsPanel.hidden = true;
    mapSection.append(alertsPanel);
  }

  function renderAlerts() {
    if (!alertsPanel) return;
    const features = (alertsData?.features || []).filter((f) => f?.properties);
    const warnings = features.filter((f) => isWarning(f.properties.event));
    const other = features.filter((f) => !isWarning(f.properties.event));
    const counts = { warnings: warnings.length, other: other.length };
    for (const tab of alertsPanel.querySelectorAll('.v4-seg')) {
      const on = tab.dataset.tab === alertsTab;
      tab.setAttribute('aria-selected', on ? 'true' : 'false');
      tab.querySelector('.v4-seg-count').textContent = String(counts[tab.dataset.tab]);
    }
    alertsPanel.querySelector('.v4-alerts-updated').textContent = alertsData?.updated
      ? `Updated ${relativeAge(alertsData.updated)}` : (alertsData ? '' : 'Loading…');

    const styles = alertsData?.styles || {};
    const list = alertsPanel.querySelector('.v4-alerts-list');
    const shown = (alertsTab === 'warnings' ? warnings : other)
      .slice()
      .sort((a, b) => (styles[b.properties.event]?.z || 0) - (styles[a.properties.event]?.z || 0));
    if (!shown.length) {
      list.replaceChildren(el('li', { class: 'v4-alerts-empty', text: alertsData ? `No active ${alertsTab === 'warnings' ? 'warnings' : 'watches or advisories'}.` : '' }));
      return;
    }
    list.replaceChildren(...shown.slice(0, 60).map((feature) => {
      const p = feature.properties;
      const color = styles[p.event]?.color || p.style?.color || '#8d96a1';
      const swatch = el('span', { class: 'v4-alert-swatch', 'aria-hidden': 'true' });
      swatch.style.background = color;
      const item = el('button', { type: 'button', class: 'v4-alert', disabled: !feature.geometry }, [
        swatch,
        el('span', { class: 'v4-alert-text' }, [
          el('span', { class: 'v4-alert-title', text: p.display_event || p.event || 'Alert' }),
          el('span', { class: 'v4-alert-area', text: p.area_desc || '' }),
        ]),
        el('span', { class: 'v4-alert-left', text: timeLeft(p.expires) }),
      ]);
      item.title = p.headline || '';
      item.addEventListener('click', function onAlertClick() { zoomToFeature(feature); });
      return el('li', {}, [item]);
    }));
  }

  async function refreshAlerts() {
    try {
      const resp = await fetch('/api/alerts');
      if (resp.ok) alertsData = await resp.json();
    } catch (err) {
      // Keep the last good list; the next refresh retries.
    }
    renderAlerts();
  }

  function syncAlertsVisibility() {
    if (!alertsPanel) return;
    const on = mapSection.classList.contains('radar-mode');
    alertsPanel.hidden = !on;
    document.documentElement.classList.toggle('v4-alerts-open', on);
    if (on && !alertsTimer) {
      refreshAlerts();
      alertsTimer = setInterval(function refreshAlertsTick() {
        if (!document.hidden) refreshAlerts();
      }, ALERT_REFRESH_MS);
    } else if (!on && alertsTimer) {
      clearInterval(alertsTimer);
      alertsTimer = null;
    }
  }

  /* ── Wire up ─────────────────────────────────────────────── */

  buildModelChips();
  buildRunCard();
  buildReadout();
  buildAlertsPanel();
  syncAlertsVisibility();
  if (mapSection) {
    new MutationObserver(syncAlertsVisibility).observe(mapSection, { attributes: true, attributeFilter: ['class'] });
  }
  syncModelChips();
  syncRunCard();

  modelSelect?.addEventListener('change', syncModelChips);
  runSelect?.addEventListener('change', syncRunCard);
  // app.js repopulates runs after every model change and sets values directly.
  if (runSelect) {
    new MutationObserver(function onRunOptionsChanged() {
      syncModelChips();
      syncRunCard();
      queueMicrotask(syncRunCard);
    }).observe(runSelect, { childList: true });
  }
  // Values set programmatically (URL restore, run selection) fire no events.
  setInterval(function syncFromAppState() {
    if (document.hidden) return;
    syncModelChips();
    syncRunCard();
  }, 1000);
})();
