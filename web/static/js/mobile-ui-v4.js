'use strict';

/**
 * WX Viewer phone — v4 dock (only when <html data-ui="v4">).
 * Groups the layer button, legend and player into one bottom dock.
 * The original nodes move intact, so mobile.js keeps its references
 * and listeners.
 */
(function initMobileUiV4() {
  if (document.documentElement.dataset.ui !== 'v4') return;
  const parts = ['selectorPill', 'legendBar', 'scrubber']
    .map((id) => document.getElementById(id))
    .filter(Boolean);
  if (!parts.length) return;
  const dock = document.createElement('section');
  dock.className = 'v4-dock';
  dock.setAttribute('aria-label', 'Layer and time');
  parts[0].parentNode.insertBefore(dock, parts[0]);
  dock.append(...parts);
})();
