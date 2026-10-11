/* nnign Service Worker: App-Hülle offline, Seiten „Netz zuerst“, zuletzt gesehene Seiten als Rückfall. */
const VERSION = 'nnign-__VERSION__';
const SHELL = ['/offline', '/static/style.css?v=__VERSION__', '/static/logo.svg', '/static/icons/icon-192.png',
               '/static/icons/icon-512.png', '/static/icons/favicon-32.png'];
const NO_CACHE = /^\/(admin|login|logout|api|fragment|export\.csv|docs|openapi\.json|healthz)/;

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(VERSION).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  const url = new URL(req.url);
  if (req.method !== 'GET' || url.origin !== location.origin) return;
  if (url.pathname.startsWith('/static/')) {          // statische Dateien: Cache zuerst
    e.respondWith(caches.match(req).then((hit) => hit || fetch(req).then((res) => {
      if (res.ok) { const copy = res.clone(); caches.open(VERSION).then((c) => c.put(req, copy)); }
      return res;
    })));
    return;
  }
  if (NO_CACHE.test(url.pathname)) return;           // Admin, API, Live-Daten: immer frisch
  if (req.mode === 'navigate') {                     // Seiten: Netz zuerst, sonst letzte Fassung oder Offline-Seite
    e.respondWith(fetch(req).then((res) => {
      if (res.ok) { const copy = res.clone(); caches.open(VERSION).then((c) => c.put(req, copy)); }
      return res;
    }).catch(() => caches.match(req).then((hit) => hit || caches.match('/offline'))));
  }
});
