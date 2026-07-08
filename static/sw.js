// Service Worker mínimo: cachea solo el app shell (HTML/manifest/icon).
// NO cachea respuestas de /api/* — el proxy debe ser siempre en vivo.
const CACHE = 'mediadl-v1';
const SHELL = ['/', '/static/manifest.json', '/static/icon.svg'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  // Estrategia network-first para el shell; las APIs nunca se cachean.
  if (url.pathname.startsWith('/api/')) return;
  e.respondWith(
    fetch(e.request).then((resp) => {
      const copy = resp.clone();
      caches.open(CACHE).then((c) => c.put(e.request, copy));
      return resp;
    }).catch(() => caches.match(e.request).then((r) => r || caches.match('/')))
  );
});