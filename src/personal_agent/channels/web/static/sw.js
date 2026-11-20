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
  "/workspace",
  "/static/workbench.js",
  "/static/workbench.css",
  "/static/orchestration.js",
  "/static/orchestration.css",
  "/static/assistant.js",
  "/static/assistant.css",
  `/static/style.css?v=${VERSION}`,
  `/static/app.js?v=${VERSION}`,
  "/static/api.js",
  "/static/query.js",
  "/static/markdown-parser.js",
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
// Only explicitly public shell paths may enter storage. A blacklist of /api/ misses encoded
// forms such as /%61pi/history, which the server decodes into an authenticated API route.
const SHELL_PATHS = new Set(SHELL.map((path) => new URL(path, self.location.origin).pathname));

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
  if (!SHELL_PATHS.has(url.pathname)) return; // private, unknown and encoded paths: network only
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
