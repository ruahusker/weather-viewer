const CACHE_NAME = 'wx-pwa-v39';
const SHELL_ASSETS = [
  '/static/mobile.html',
  '/static/css/mobile.css?v=46',
  '/static/css/surface-obs.css?v=20260330b',
  '/static/css/mobile-refresh.css?v=20260528b',
  '/static/js/mobile.js?v=20260528b',
  '/static/js/mobile-sounding.js?v=43',
  '/static/js/surface-obs.js?v=20260331',
  '/static/manifest.json',
  '/static/brand/wx-mark.svg',
  '/static/icons/icon-192.png',
  'https://unpkg.com/leaflet@1.9.4/dist/leaflet.css',
  'https://unpkg.com/leaflet@1.9.4/dist/leaflet.js',
];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE_NAME).then((c) => c.addAll(SHELL_ASSETS)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);

  // Rendered weather images & tiles — cache-first (immutable filenames)
  if (url.pathname.includes('/rendered/') || url.pathname.includes('/satellite/') ||
      url.hostname.includes('basemaps.cartocdn.com')) {
    e.respondWith(
      caches.open(CACHE_NAME).then((c) =>
        c.match(e.request).then((r) => r || fetch(e.request).then((resp) => {
          if (resp.ok) c.put(e.request, resp.clone());
          return resp;
        }))
      )
    );
    return;
  }

  // API — network-first, short cache fallback
  if (url.pathname.includes('/api/')) {
    e.respondWith(
      fetch(e.request).then((resp) => {
        if (resp.ok) {
          const clone = resp.clone();
          caches.open(CACHE_NAME).then((c) => c.put(e.request, clone));
        }
        return resp;
      }).catch(() => caches.match(e.request))
    );
    return;
  }

  // HTML pages — network-first so updates are immediate
  if (e.request.mode === 'navigate' || url.pathname.endsWith('.html')) {
    e.respondWith(
      fetch(e.request).then((resp) => {
        if (resp.ok) { const c = resp.clone(); caches.open(CACHE_NAME).then((cache) => cache.put(e.request, c)); }
        return resp;
      }).catch(() => caches.match(e.request))
    );
    return;
  }

  // App shell assets (css/js with ?v= busting) — cache-first
  e.respondWith(
    caches.match(e.request).then((r) => r || fetch(e.request).then((resp) => {
      if (resp.ok) { const c = resp.clone(); caches.open(CACHE_NAME).then((cache) => cache.put(e.request, c)); }
      return resp;
    }))
  );
});
