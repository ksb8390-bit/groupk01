// 앱 셸만 캐시 (근태 데이터는 항상 서버에서 조회)
const CACHE = 'gk-hr-v2';
const SHELL = ['/static/icon-192.png', '/static/icon-512.png'];
self.addEventListener('install', e => e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting())));
self.addEventListener('activate', e => e.waitUntil(
  caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim())));
self.addEventListener('fetch', e => {
  const u = new URL(e.request.url);
  if (e.request.method !== 'GET' || !u.pathname.startsWith('/static/') || u.pathname.endsWith('.css')) return;
  e.respondWith(caches.match(e.request).then(r => r || fetch(e.request)));
});
