// Keeps the page's own files, so the page opens when the connection does not.
//
// It never touches /api/: the conversation, the pictures and the live events always come from the
// server, fresh, or not at all. A page that opened offline says it cannot reach the agent; it does
// not show an old copy of anything private. The cache holds only what is public anyway: the page,
// its script and style, and its icons.

const VERSION = "{{version}}";
const CACHE = `pa-shell-${VERSION}`;
const SHELL = [
  "/",
  `/static/style.css?v=${VERSION}`,
  `/static/app.js?v=${VERSION}`,
  "/static/api.js",
  "/static/activity.js",
  "/static/audio-metadata.js",
  "/static/format.js",
  "/static/md.js",
  "/static/media.js",
  "/static/store.js",
  "/static/icon.svg",
  "/static/icon-192.png",
  "/manifest.webmanifest",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE)
      .then((cache) => cache.addAll(SHELL.map((url) => new Request(url, { cache: "reload" }))))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((names) =>
        Promise.all(names.filter((name) => name.startsWith("pa-shell-") && name !== CACHE).map((name) => caches.delete(name))),
      )
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/") || url.pathname === "/sw.js") return; // the network, or nothing
  event.respondWith(networkFirst(request));
});

async function networkFirst(request) {
  const cache = await caches.open(CACHE);
  try {
    const fresh = await fetch(request);
    if (fresh.ok) cache.put(request, fresh.clone());
    return fresh;
  } catch {
    const kept =
      (await cache.match(request)) ??
      (request.mode === "navigate" ? await cache.match("/") : undefined);
    return kept ?? new Response("Offline", { status: 503, headers: { "Content-Type": "text/plain" } });
  }
}
