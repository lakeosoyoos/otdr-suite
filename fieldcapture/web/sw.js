/* Service worker: keeps the app available with no signal.
   App files are network first, so a new version is picked up whenever the phone is online.
   Libraries, the label reader and the blank FQA form never change under the same name,
   so they are served from the cache first. Bump CACHE to ship an update. */
const CACHE = 'otdr-field-capture-v2';
const SHELL = ['./', './index.html', './app.css', './app.js', './fqa.js', './labels.js', './manifest.webmanifest',
  './vendor/exceljs.min.js', './vendor/jszip.min.js', './vendor/tesseract/tesseract.min.js',
  './icons/icon-192.png', './icons/icon-512.png', './icons/apple-touch-icon.png', './icons/maskable-512.png'];
const STATIC = /\/(vendor|fqa|icons)\//;

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});
function fromNetwork(req) {
  return fetch(req).then((resp) => {
    if (resp.ok) { const copy = resp.clone(); caches.open(CACHE).then((c) => c.put(req, copy)); }
    return resp;
  });
}
self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin) return;
  if (STATIC.test(url.pathname)) {
    e.respondWith(caches.match(e.request, { ignoreSearch: true }).then((hit) => hit || fromNetwork(e.request)));
    return;
  }
  e.respondWith(fromNetwork(e.request).catch(() => caches.match(e.request, { ignoreSearch: true }).then((hit) => hit || caches.match('./index.html'))));
});
