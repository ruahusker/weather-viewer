'use strict';
/* ═══════════════════════════════════════════════════════════════════
   surface-obs.js — Shared IEM Mesonet surface observation renderer
   Used by both app.js (desktop) and mobile.js

   API:
     SurfaceObs.init(leafletMap, BASE)     — call once after map is ready
     SurfaceObs.show()                     — show layer, start auto-refresh
     SurfaceObs.hide()                     — hide layer, stop auto-refresh
     SurfaceObs.isVisible()                — boolean
     SurfaceObs.setPayload(payload)        — inject preloaded frame payload
     SurfaceObs.setRadarOverlay(bool)      — used on radar tab overlay toggle
     SurfaceObs.destroy()                  — clean up

   Zoom tiers (station density):
     tier 1 (major hubs)       national view
     tier 2 (primary ASOS)     regional view
     tier 3 (everything else)  local view
   ═══════════════════════════════════════════════════════════════════ */

const SurfaceObs = (() => {
  // ─── Constants ────────────────────────────────────────────────
  const ZOOM_TIER2 = 4.25;
  const ZOOM_TIER3 = 6.5;
  const REFRESH_MS = 5 * 60 * 1000; // 5 minutes
  const STALE_SEC  = 90 * 60;        // 90 min → grey out station
  const PANE_ZINDEX = 490;

  // ─── State ────────────────────────────────────────────────────
  let _map = null;
  let _base = '';
  let _pane = null;
  let _group = null;          // L.LayerGroup for all station markers
  let _stations = [];         // raw array from API
  let _generated = 0;         // epoch of dataset
  let _visible = false;
  let _requestId = 0;
  let _radarOverlay = false;  // when true, obs shows on radar tab as overlay
  let _refreshTimer = null;
  let _activeMarker = null;   // currently open detail panel marker
  let _detailPanel = null;    // DOM element for click-to-detail

  function _isMobileView() {
    try {
      return window.matchMedia('(max-width: 768px)').matches;
    } catch {
      return window.innerWidth <= 768;
    }
  }

  // ─── Wind barb SVG ────────────────────────────────────────────
  /**
   * Render a meteorological wind barb as an SVG string.
   * @param {number|null} wdir  Wind direction in degrees (meteorological: FROM)
   * @param {number|null} wspd  Wind speed in mph
   * @param {string} color
   * @returns {string} SVG markup
   */
  function _windBarbSvg(wdir, wspd, color = '#ffffff') {
    const sz = 28;
    const cx = sz / 2, cy = sz / 2;

    // Calm wind: open circle
    if (wspd == null || wdir == null || wspd < 1.5) {
      return `<svg width="${sz}" height="${sz}" viewBox="0 0 ${sz} ${sz}" xmlns="http://www.w3.org/2000/svg">
        <circle cx="${cx}" cy="${cy}" r="4" fill="none" stroke="${color}" stroke-width="1.5"/>
      </svg>`;
    }

    // Convert mph → knots for barb notation
    const kts = wspd / 1.15078;

    // Build barb elements:
    // - Staff: drawn from station dot outward in wind direction
    // - Barbs/flags on the "top" end (away from station)
    // Direction: wind comes FROM wdir. Staff points INTO wind (FROM direction).
    // In meteorological convention the barb is drawn with the staff pointing
    // into the wind and barbs/flags on the staff.

    const rad = wdir * (Math.PI / 180); // staff points INTO the wind (FROM direction)
    // shaft runs from center toward the "from" direction
    const shaftLen = 11;
    const tx = cx + Math.sin(rad) * shaftLen;
    const ty = cy - Math.cos(rad) * shaftLen;

    // Perpendicular direction for barbs (to the right of shaft looking outward)
    const perpRad = rad + Math.PI / 2;

    const lines = [];

    // Station dot
    lines.push(`<circle cx="${cx}" cy="${cy}" r="2.2" fill="${color}"/>`);

    // Shaft
    lines.push(`<line x1="${cx.toFixed(1)}" y1="${cy.toFixed(1)}" x2="${tx.toFixed(1)}" y2="${ty.toFixed(1)}" stroke="${color}" stroke-width="1.5" stroke-linecap="round"/>`);

    // Barb segments along shaft
    let remaining = kts;
    let pos = 0; // distance from tip (tx,ty) back toward center

    const stepLen = 2.6;   // spacing between barbs
    const barbLen = 7;      // full barb length
    const shortLen = 3.5;   // half barb

    // Pennants (50 kts each)
    while (remaining >= 47.5) {
      const bx = tx - Math.sin(rad) * pos;
      const by = ty + Math.cos(rad) * pos;
      const b1x = bx + Math.sin(perpRad) * barbLen;
      const b1y = by - Math.cos(perpRad) * barbLen;
      const b2x = bx - Math.sin(rad) * stepLen;
      const b2y = by + Math.cos(rad) * stepLen;
      lines.push(`<polygon points="${bx.toFixed(1)},${by.toFixed(1)} ${b1x.toFixed(1)},${b1y.toFixed(1)} ${b2x.toFixed(1)},${b2y.toFixed(1)}" fill="${color}"/>`);
      pos += stepLen * 1.5;
      remaining -= 50;
    }

    // Full barbs (10 kts each)
    while (remaining >= 7.5) {
      const bx = tx - Math.sin(rad) * pos;
      const by = ty + Math.cos(rad) * pos;
      const b1x = bx + Math.sin(perpRad) * barbLen;
      const b1y = by - Math.cos(perpRad) * barbLen;
      lines.push(`<line x1="${bx.toFixed(1)}" y1="${by.toFixed(1)}" x2="${b1x.toFixed(1)}" y2="${b1y.toFixed(1)}" stroke="${color}" stroke-width="1.5" stroke-linecap="round"/>`);
      pos += stepLen;
      remaining -= 10;
    }

    // Half barb (5 kts)
    if (remaining >= 2.5) {
      const bx = tx - Math.sin(rad) * pos;
      const by = ty + Math.cos(rad) * pos;
      const b1x = bx + Math.sin(perpRad) * shortLen;
      const b1y = by - Math.cos(perpRad) * shortLen;
      lines.push(`<line x1="${bx.toFixed(1)}" y1="${by.toFixed(1)}" x2="${b1x.toFixed(1)}" y2="${b1y.toFixed(1)}" stroke="${color}" stroke-width="1.5" stroke-linecap="round"/>`);
    }

    return `<svg width="${sz}" height="${sz}" viewBox="0 0 ${sz} ${sz}" xmlns="http://www.w3.org/2000/svg">${lines.join('')}</svg>`;
  }

  // ─── Stale check ─────────────────────────────────────────────
  function _isStale(validStr) {
    if (!validStr) return true;
    try {
      const d = new Date(validStr);
      const age = (Date.now() / 1000) - (d.getTime() / 1000);
      return age > STALE_SEC;
    } catch { return true; }
  }

  // ─── Build Leaflet icon for one station ──────────────────────
  function _makeIcon(stn) {
    const stale = _isStale(stn.valid);
    const tColor = stale ? '#666' : '#ef4444';
    const dColor = stale ? '#666' : '#22c55e';
    const barbColor = stale ? '#555' : '#e0e0e0';
    const barbSvg = _windBarbSvg(stn.wdir, stn.wspd, barbColor);
    const tempLabel = stn.t != null ? Math.round(stn.t) + '°' : '--';
    const dewpointLabel = stn.d != null ? Math.round(stn.d) + '°' : '--';

    const html = `<div class="wx-stn-icon${stale ? ' stale' : ''}">
      <div class="wx-stn-temp" style="color:${tColor}">${tempLabel}</div>
      <div class="wx-stn-dewpoint" style="color:${dColor}">${dewpointLabel}</div>
      <div class="wx-stn-barb">${barbSvg}</div>
    </div>`;

    return L.divIcon({
      html,
      className: '',
      iconSize:   [42, 42],
      iconAnchor: [21, 21],
    });
  }

  // ─── Build detail panel HTML ──────────────────────────────────
  function _detailHtml(stn) {
    const dir = stn.wdir != null ? `${stn.wdir}°` : '--';
    const spd = stn.wspd != null ? `${Math.round(stn.wspd)} mph` : '--';
    const gst = stn.wgst != null ? ` / gust ${Math.round(stn.wgst)} mph` : '';
    const dewStr = stn.d != null ? `${Math.round(stn.d)}°F` : '--';
    const visStr = stn.vsby != null ? `${stn.vsby} mi` : '--';
    const mslpStr = stn.mslp != null ? `${stn.mslp.toFixed(1)} mb` : '--';
    const sky = stn.sky || '--';
    const wx  = stn.wx  || '';
    const validStr = stn.valid
      ? new Date(stn.valid).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', timeZoneName:'short'})
      : '--';

    return `
      <div class="stn-detail-header">
        <strong>${stn.sid}</strong><span class="stn-detail-name">${stn.name}</span>
        <span class="stn-detail-valid">${validStr}</span>
      </div>
      <table class="stn-detail-table">
        <tr><td>Temp</td><td>${stn.t != null ? Math.round(stn.t) + '°F' : '--'}</td>
            <td>Dewpoint</td><td>${dewStr}</td></tr>
        <tr><td>Wind</td><td>${dir} @ ${spd}${gst}</td>
            <td>Visibility</td><td>${visStr}</td></tr>
        <tr><td>Sky</td><td>${sky}${wx ? ' / ' + wx : ''}</td>
            <td>MSLP</td><td>${mslpStr}</td></tr>
      </table>`;
  }

  function _cellSizeFor(stn, zoom) {
    const mobile = _isMobileView();
    if (stn.tier === 1) {
      if (zoom < 4.5) return mobile ? 84 : 108;
      if (zoom < 5.5) return mobile ? 70 : 88;
      return mobile ? 66 : 80;
    }
    if (stn.tier === 2) {
      if (zoom < 4.75) return mobile ? 112 : 150;
      if (zoom < 5.5) return mobile ? 96 : 124;
      if (zoom < 6.25) return mobile ? 82 : 102;
      return mobile ? 72 : 88;
    }
    if (zoom < 7.25) return mobile ? 92 : 122;
    if (zoom < 8.0) return mobile ? 76 : 96;
    return mobile ? 68 : 82;
  }

  function _densityFilter(candidates, zoom) {
    if (!_map || !candidates.length) return candidates;
    const size = _map.getSize();
    const pad = 64;
    const grid = new Map();
    const kept = [];

    const sorted = candidates.slice().sort((a, b) => {
      if (a.tier !== b.tier) return a.tier - b.tier;
      return String(a.sid || '').localeCompare(String(b.sid || ''));
    });

    for (const stn of sorted) {
      const pt = _map.latLngToContainerPoint([stn.lat, stn.lon]);
      if (pt.x < -pad || pt.y < -pad || pt.x > size.x + pad || pt.y > size.y + pad) continue;

      const cell = _cellSizeFor(stn, zoom);
      const key = `${Math.floor(pt.x / cell)}:${Math.floor(pt.y / cell)}`;
      if (grid.has(key)) continue;
      grid.set(key, true);
      kept.push(stn);
    }

    return kept;
  }

  // ─── Filter by zoom tier ─────────────────────────────────────
  function _visibleStations() {
    if (!_map) return [];
    const mobile = _isMobileView();
    const z = _map.getZoom();
    const tier2Zoom = mobile ? ZOOM_TIER2 - 0.85 : ZOOM_TIER2;
    const tier3Zoom = mobile ? ZOOM_TIER3 - 0.75 : ZOOM_TIER3;
    const candidates = _stations.filter(s => {
      if (s.tier === 1) return true;
      if (s.tier === 2) return z >= tier2Zoom;
      return z >= tier3Zoom;
    });
    return _densityFilter(candidates, z);
  }

  // ─── Render / re-render all markers ──────────────────────────
  function _render() {
    if (!_group || !_visible) return;
    _group.clearLayers();

    const visible = _visibleStations();
    for (const stn of visible) {
      const marker = L.marker([stn.lat, stn.lon], {
        icon: _makeIcon(stn),
        pane: 'surfaceObsPane',
        interactive: true,
      });

      marker.on('click', (e) => {
        L.DomEvent.stopPropagation(e);
        _showDetail(stn, marker);
      });

      _group.addLayer(marker);
    }
  }

  // ─── Detail panel (floating) ─────────────────────────────────
  function _showDetail(stn, marker) {
    if (!_detailPanel) return;
    _detailPanel.innerHTML = _detailHtml(stn);
    _detailPanel.classList.remove('hidden');
    _activeMarker = marker;
  }

  function _hideDetail() {
    if (_detailPanel) _detailPanel.classList.add('hidden');
    _activeMarker = null;
  }

  // ─── Fetch data from API ─────────────────────────────────────
  async function _fetch() {
    if (document.hidden) return;
    const requestId = ++_requestId;
    try {
      const r = await fetch(`${_base}/api/surface/current`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const d = await r.json();
      if (requestId !== _requestId || !_visible) return;
      _stations = d.stations || [];
      _generated = d.generated || 0;
      _render();
    } catch (err) {
      console.warn('[SurfaceObs] fetch error:', err);
    }
  }

  // ─── Auto-refresh timer ───────────────────────────────────────
  function _startRefresh() {
    _stopRefresh();
    _refreshTimer = setInterval(_fetch, REFRESH_MS);
  }

  function _stopRefresh() {
    if (_refreshTimer) { clearInterval(_refreshTimer); _refreshTimer = null; }
  }

  // ─── Zoom handler (re-render on zoom change) ──────────────────
  function _onZoom() {
    if (_visible) _render();
  }

  // ─── Public API ──────────────────────────────────────────────
  function init(leafletMap, base) {
    _map  = leafletMap;
    _base = base || '';

    // Create a dedicated pane above alerts
    if (!_map.getPane('surfaceObsPane')) {
      _pane = _map.createPane('surfaceObsPane');
      _pane.style.zIndex = PANE_ZINDEX;
      _pane.style.pointerEvents = 'none';
    } else {
      _pane = _map.getPane('surfaceObsPane');
      _pane.style.pointerEvents = 'none';
    }

    _group = L.layerGroup();

    _map.on('zoomend', _onZoom);
    _map.on('moveend', _onZoom);

    // Detail panel — look for existing element or create one
    _detailPanel = document.getElementById('stnDetailPanel');
    if (!_detailPanel) {
      _detailPanel = document.createElement('div');
      _detailPanel.id = 'stnDetailPanel';
      _detailPanel.className = 'stn-detail-panel hidden';
      document.body.appendChild(_detailPanel);
    }

    // Close detail panel when clicking elsewhere on map
    _map.on('click', _hideDetail);
  }

  function show({ autoRefresh = true } = {}) {
    if (!_map || _visible) return;
    _visible = true;
    _group.addTo(_map);
    if (_stations.length === 0 && autoRefresh) {
      _fetch();
    } else {
      _render();
    }
    if (autoRefresh) _startRefresh();
  }

  function hide() {
    if (!_map || !_visible) return;
    _visible = false;
    ++_requestId;
    _group.remove();
    _hideDetail();
    _stopRefresh();
  }

  function isVisible() { return _visible; }

  function setPayload(payload) {
    ++_requestId;
    _stations = Array.isArray(payload?.stations) ? payload.stations : [];
    _generated = Number.isFinite(payload?.generated) ? payload.generated : 0;
    if (_visible) _render();
  }

  function setRadarOverlay(on) {
    _radarOverlay = on;
    if (on) show(); else hide();
  }

  function destroy() {
    _stopRefresh();
    hide();
    if (_map) {
      _map.off('zoomend', _onZoom);
      _map.off('click', _hideDetail);
    }
    _map = null;
    _group = null;
    _stations = [];
  }

  // ─── Screenshot helper ────────────────────────────────────────
  // Draws the currently-visible obs directly onto an existing Canvas 2D context.
  // Called by the screenshot handler in mobile.js. Uses internal state directly
  // (no DOM querying, no foreignObject) so it works safely on iOS Safari.
  // ctx must already have ctx.scale(2,2) applied so 1 ctx unit == 1 CSS pixel.
  async function drawToCanvas(ctx) {
    if (!_visible || !_map || !_stations.length) return;
    const visible = _visibleStations();

    // Build render data + pre-load wind barb SVGs in parallel
    const entries = visible.map((stn) => {
      const stale    = _isStale(stn.valid);
      const tColor   = stale ? '#666' : '#ef4444';
      const dColor   = stale ? '#666' : '#22c55e';
      const barbSvg  = _windBarbSvg(stn.wdir, stn.wspd, stale ? '#555' : '#e0e0e0');
      const tempLbl  = stn.t != null ? Math.round(stn.t) + '\u00b0' : '--';
      const dewLbl   = stn.d != null ? Math.round(stn.d) + '\u00b0' : '--';
      // latLngToContainerPoint returns CSS-pixel coords relative to map container
      const pt = _map.latLngToContainerPoint([stn.lat, stn.lon]);
      const mx = pt.x - 21; // iconAnchor is [21,21]
      const my = pt.y - 21;
      return { mx, my, stale, tColor, dColor, barbSvg, tempLbl, dewLbl };
    });

    const barbImgs = await Promise.all(entries.map(async ({ barbSvg }) => {
      try {
        const img = new Image();
        await new Promise((res) => {
          img.onload = res;
          img.onerror = res;
          img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(barbSvg);
        });
        return img;
      } catch { return null; }
    }));

    for (let i = 0; i < entries.length; i++) {
      const { mx, my, stale, tColor, dColor, tempLbl, dewLbl } = entries[i];
      const barbImg = barbImgs[i];

      // Wind barb (top:7px left:14px 28×28)
      if (barbImg) {
        ctx.save();
        if (stale) ctx.globalAlpha = 0.4;
        ctx.drawImage(barbImg, mx + 14, my + 7, 28, 28);
        ctx.restore();
      }

      // Temp (top:9px, right-edge of 16px col → baseline ≈ my+20)
      ctx.save();
      ctx.font = 'bold 11px -apple-system, sans-serif';
      ctx.fillStyle = tColor;
      ctx.textAlign = 'right';
      ctx.shadowColor = '#000';
      ctx.shadowBlur = 4;
      ctx.fillText(tempLbl, mx + 16, my + 20);
      ctx.restore();

      // Dewpoint (top:18px → baseline ≈ my+29)
      ctx.save();
      ctx.font = 'bold 11px -apple-system, sans-serif';
      ctx.fillStyle = dColor;
      ctx.textAlign = 'right';
      ctx.shadowColor = '#000';
      ctx.shadowBlur = 4;
      ctx.fillText(dewLbl, mx + 16, my + 29);
      ctx.restore();
    }
  }

  return { init, show, hide, isVisible, setPayload, setRadarOverlay, destroy, drawToCanvas };
})();
