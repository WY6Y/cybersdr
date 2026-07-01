/*
 * service-worker.js — CyberSDR offline shell cache.
 *
 * Strategy: network-first for all requests; fall back to cache for
 * the dashboard shell so the UI loads even when the Pi is unreachable.
 */

const CACHE_VERSION = 'cybersdr-v1';
const SHELL_ASSETS = [
  '/',
  '/static/js/app.js',
  '/static/manifest.json',
];

// ── Install: pre-cache the shell ──────────────────────────────────────────────
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_VERSION).then(cache => {
      return cache.addAll(SHELL_ASSETS);
    }).then(() => self.skipWaiting())
  );
});

// ── Activate: purge old caches ────────────────────────────────────────────────
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(
        keys.filter(k => k !== CACHE_VERSION).map(k => caches.delete(k))
      )
    ).then(() => self.clients.claim())
  );
});

// ── Fetch: network-first, cache fallback ──────────────────────────────────────
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);

  // Never intercept SSE stream or API calls — always hit network
  if (url.pathname === '/stream' || url.pathname.startsWith('/api/')) {
    return;
  }

  // GET requests only
  if (event.request.method !== 'GET') return;

  event.respondWith(
    fetch(event.request)
      .then(response => {
        // Cache successful responses for shell assets
        if (response.ok && SHELL_ASSETS.some(a => url.pathname === a || url.pathname === '/')) {
          const clone = response.clone();
          caches.open(CACHE_VERSION).then(c => c.put(event.request, clone));
        }
        return response;
      })
      .catch(() => caches.match(event.request))
  );
});
