'use strict';

/** Pressure vectors and annotations shared by the desktop and phone maps. */
(() => {
  const cache = new Map();
  const pending = new Map();
  const CACHE_LIMIT = 24;
  const CONTOUR_COLOR = '#7dd3e8';
  const CONTOUR_WIDTHS = { major: 1.8, minor: 0.8 };
  const isUrl = (url) => /\.contours\.json(?:[?#]|$)/.test(url || '');

  /** Reuse downloads across the frame preloader and the visible layer. */
  async function load(url) {
    if (cache.has(url)) {
      const data = cache.get(url);
      cache.delete(url);
      cache.set(url, data);
      return data;
    }
    if (pending.has(url)) return pending.get(url);
    const request = (async () => {
      const response = await fetch(url);
      if (!response.ok) throw new Error(`Pressure contours: ${response.status}`);
      const data = await response.json();
      if (data.type !== 'FeatureCollection' || !Array.isArray(data.features)) {
        throw new Error('Invalid pressure contour data');
      }
      cache.set(url, data);
      while (cache.size > CACHE_LIMIT) cache.delete(cache.keys().next().value);
      return data;
    })();
    pending.set(url, request);
    try { return await request; }
    finally { pending.delete(url); }
  }

  const PressureLayer = L.LayerGroup.extend({
    initialize(url, options) {
      L.LayerGroup.prototype.initialize.call(this, [], options);
      this.isPressureContours = true;
      this._url = url;
      this._requestId = 0;
      this._opacity = 1;
      this._loaded = false;
    },

    onAdd(map) {
      const paneName = 'pressureContoursPane';
      let pane = map.getPane(paneName);
      if (!pane) {
        pane = map.createPane(paneName);
        pane.style.zIndex = '420';
        pane.style.pointerEvents = 'none';
      }
      this.options.pane = paneName;
      this._renderer = L.svg({ pane: paneName }).addTo(map);
      L.LayerGroup.prototype.onAdd.call(this, map);
      this.setUrl(this._url);
    },

    onRemove(map) {
      ++this._requestId;
      this._loading = null;
      this._loaded = false;
      this.clearLayers();
      this._data = null;
      L.LayerGroup.prototype.onRemove.call(this, map);
      this._renderer.remove();
      this._renderer = null;
    },

    /** Keep the image-overlay interface used by frame switching and playback. */
    getElement() { return this._renderer?._container || null; },
    setBounds() { return this; },

    setOpacity(opacity) {
      this._opacity = opacity;
      this._lines?.setStyle((feature) => ({ opacity: feature.properties.opacity * opacity }));
      this.eachLayer((layer) => {
        if (layer instanceof L.Marker) layer.setOpacity(opacity);
      });
      return this;
    },

    setUrl(url) {
      if (url === this._url && (this._loaded || this._loading)) return this;
      this._url = url;
      if (!this._map) return this;
      const requestId = ++this._requestId;
      const element = this.getElement();
      element.complete = false;
      this._loaded = false;
      this._loading = load(url).then((data) => {
        if (!this._map || requestId !== this._requestId) return;
        this.clearLayers();
        this._data = data;
        this._lines = L.geoJSON(data, {
          pane: this.options.pane,
          renderer: this._renderer,
          interactive: false,
          smoothFactor: 0.3,
          filter: (feature) => feature.properties.kind === 'isobar',
          style: (feature) => ({
            color: CONTOUR_COLOR, weight: feature.properties.major ? CONTOUR_WIDTHS.major : CONTOUR_WIDTHS.minor,
            opacity: feature.properties.opacity * this._opacity, fill: false,
          }),
        });
        this.addLayer(this._lines);
        for (const feature of data.features) {
          if (feature.geometry.type === 'Point') this._addLabel(feature);
        }
        this._loaded = true;
        element.complete = true;
        element.dispatchEvent(new Event('load'));
        this.fire('load');
      }).catch((error) => {
        if (!this._map || requestId !== this._requestId) return;
        this.clearLayers();
        this._data = null;
        element.complete = true;
        element.dispatchEvent(new Event('error'));
        this.fire('error', { error });
      }).finally(() => {
        if (requestId === this._requestId) this._loading = null;
      });
      return this;
    },

    _addLabel(feature) {
      const props = feature.properties;
      const value = Number(props.value);
      if (!Number.isFinite(value)) return;
      let html;
      if (props.kind === 'center' && ['H', 'L'].includes(props.symbol)) {
        html = `<span class="pressure-center pressure-center-${props.symbol.toLowerCase()}">${props.symbol}<small>${Math.round(value)}</small></span>`;
      } else if (props.kind === 'label') {
        const rotation = Number.isFinite(props.rotation) ? props.rotation % 360 : 0;
        html = `<span class="pressure-number" style="transform:translate(-50%,-50%) rotate(${rotation}deg)">${Math.round(value)}</span>`;
      } else return;
      const [lon, lat] = feature.geometry.coordinates;
      this.addLayer(L.marker([lat, lon], {
        pane: this.options.pane, interactive: false, keyboard: false,
        zIndexOffset: 1000, opacity: this._opacity,
        icon: L.divIcon({ className: 'pressure-label', html, iconSize: [0, 0], iconAnchor: [0, 0] }),
      }));
    },

    /** Draw geographic paths and fixed-size labels at the current viewport. */
    drawToCanvas(ctx) {
      if (!this._map || !this._data) return;
      const project = ([lon, lat]) => this._map.latLngToContainerPoint([lat, lon]);
      ctx.save();
      ctx.lineJoin = 'round';
      ctx.lineCap = 'round';
      for (const feature of this._data.features) {
        const props = feature.properties;
        if (props.kind !== 'isobar') continue;
        ctx.strokeStyle = CONTOUR_COLOR;
        ctx.lineWidth = props.major ? CONTOUR_WIDTHS.major : CONTOUR_WIDTHS.minor;
        ctx.globalAlpha = props.opacity * this._opacity;
        for (const line of feature.geometry.coordinates) {
          ctx.beginPath();
          line.forEach((coordinate, index) => {
            const point = project(coordinate);
            if (index === 0) ctx.moveTo(point.x, point.y);
            else ctx.lineTo(point.x, point.y);
          });
          ctx.stroke();
        }
      }
      ctx.globalAlpha = this._opacity;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      for (const feature of this._data.features) {
        const props = feature.properties;
        if (feature.geometry.type !== 'Point') continue;
        const point = project(feature.geometry.coordinates);
        ctx.save();
        ctx.translate(point.x, point.y);
        if (props.kind === 'label') {
          ctx.rotate((props.rotation || 0) * Math.PI / 180);
          ctx.font = '400 9px Arial';
          const width = ctx.measureText(String(props.value)).width + 4;
          ctx.fillStyle = 'rgba(0,0,0,0.65)';
          ctx.fillRect(-width / 2, -5.5, width, 11);
          ctx.fillStyle = CONTOUR_COLOR;
          ctx.fillText(String(props.value), 0, 0);
        } else if (props.kind === 'center') {
          ctx.shadowColor = 'rgba(0,0,0,0.8)';
          ctx.shadowBlur = 2;
          ctx.font = '400 12px Arial';
          ctx.fillStyle = props.symbol === 'H' ? '#66ff66' : '#ff6666';
          ctx.fillText(props.symbol, 0, -5);
          ctx.font = '400 8px Arial';
          ctx.fillStyle = '#ffffff';
          ctx.fillText(String(props.value), 0, 7);
        }
        ctx.restore();
      }
      ctx.restore();
    },

    /** Replace SVG and HTML markers temporarily for both screenshot exporters. */
    prepareCapture() {
      if (!this._map || !this._data) return null;
      const mapEl = this._map.getContainer();
      const pane = this._map.getPane(this.options.pane);
      const ratio = window.devicePixelRatio || 1;
      const origin = this._map.containerPointToLayerPoint([0, 0]);
      const canvas = document.createElement('canvas');
      canvas.width = Math.round(mapEl.clientWidth * ratio);
      canvas.height = Math.round(mapEl.clientHeight * ratio);
      Object.assign(canvas.style, {
        position: 'absolute', left: `${origin.x}px`, top: `${origin.y}px`,
        width: `${mapEl.clientWidth}px`, height: `${mapEl.clientHeight}px`, pointerEvents: 'none',
      });
      const ctx = canvas.getContext('2d');
      if (!ctx) return null;
      ctx.scale(ratio, ratio);
      this.drawToCanvas(ctx);
      const originals = [...pane.children].map((child) => [child, child.style.visibility]);
      originals.forEach(([child]) => { child.style.visibility = 'hidden'; });
      pane.appendChild(canvas);
      return () => {
        originals.forEach(([child, visibility]) => { child.style.visibility = visibility; });
        canvas.remove();
      };
    },
  });

  window.PressureContours = { isUrl, load, layer: (url) => new PressureLayer(url) };
})();
