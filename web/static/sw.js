'use strict';

const CACHE_NAME = 'wx-pwa-v60';
const IMAGE_CACHE = `${CACHE_NAME}-images`;
const MAX_IMAGES = 96;
const MAX_SHELL_ENTRIES = 80;
const SHELL_ASSETS = [
  '/static/mobile.html',
  '/static/css/fonts.css?v=20260809a',
  '/static/css/tokens.css?v=20260930e',
  '/static/css/mobile.css?v=20260610a',
  '/static/css/surface-obs.css?v=20260330b',
  '/static/css/mobile-refresh.css?v=20261008g',
  '/static/css/pressure-contours.css?v=20261008d',
  '/static/js/pressure-contours.js?v=20261008f',
  '/static/js/map-capture.js?v=20261008e',
  '/static/js/viewer-ui.js?v=20261008g',
  '/static/js/mobile.js?v=20261018-ptype2',
  '/static/js/mobile-sounding.js?v=43',
  '/static/js/surface-obs.js?v=20261014a',
  '/static/manifest.json',
  '/static/brand/wx-mark.svg',
  '/static/icons/icon-192.png',
  '/static/vendor/leaflet/leaflet.css?v=1.9.4',
  '/static/vendor/leaflet/leaflet.js?v=1.9.4',
];

/** Cache successful responses without letting a weather loop fill device storage. */
async function saveResponse(cacheName, request, response, limit) {
  if (!response.ok) return;
  try {
    const cache = await caches.open(cacheName);
    await cache.put(request, response);
    const keys = await cache.keys();
    await Promise.all(keys.slice(0, Math.max(0, keys.length - limit)).map((key) => cache.delete(key)));
  } catch {
    // Storage quota or private browsing must not break the network response.
  }
}

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE_NAME)
    .then((cache) => cache.addAll(SHELL_ASSETS))
    .then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(caches.keys().then((keys) => Promise.all(
    keys.filter((key) => key.startsWith('wx-pwa-') && ![CACHE_NAME, IMAGE_CACHE].includes(key))
      .map((key) => caches.delete(key))
  )).then(() => self.clients.claim()));
});

self.addEventListener('fetch', (event) => {
  const { request } = event;
  const url = new URL(request.url);
  if (request.method !== 'GET' || url.origin !== self.location.origin) return;

  // Weather metadata and latest aliases change in place. Never replay stale
  // warnings or forecasts as if they were a fresh API response when offline.
  if (url.pathname.includes('/api/')) {
    event.respondWith(fetch(request).catch(() => new Response(
      JSON.stringify({ detail: 'Offline: current weather data is unavailable' }),
      { status: 503, headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' } }
    )));
    return;
  }

  const weatherAsset = /\/(rendered|satellite|radar)\//.test(url.pathname);
  if (weatherAsset && (url.pathname.endsWith('.json') || /latest/i.test(url.pathname))) {
    event.respondWith(fetch(request, { cache: 'no-cache' }).catch(() => Response.error()));
    return;
  }

  const navigation = request.mode === 'navigate' || url.pathname.endsWith('.html');
  if (navigation) {
    event.respondWith(fetch(request).then((response) => {
      event.waitUntil(saveResponse(CACHE_NAME, request, response.clone(), MAX_SHELL_ENTRIES));
      return response;
    }).catch(async () => (await caches.match(request, { cacheName: CACHE_NAME }))
      || (await caches.match('/static/mobile.html', { cacheName: CACHE_NAME }))
      || new Response('Offline. Reconnect to load WX Viewer.', { status: 503 })));
    return;
  }

  // Only our static assets and immutable weather PNGs belong in these caches.
  if (!weatherAsset && !url.pathname.startsWith('/static/')) return;
  const cacheName = weatherAsset ? IMAGE_CACHE : CACHE_NAME;
  const limit = weatherAsset ? MAX_IMAGES : MAX_SHELL_ENTRIES;
  event.respondWith(caches.open(cacheName).then(async (cache) => {
    const cached = await cache.match(request);
    if (cached) return cached;
    const response = await fetch(request);
    event.waitUntil(saveResponse(cacheName, request, response.clone(), limit));
    return response;
  }));
});
