// The service worker, run against a pretend browser: what it keeps, what it never touches, and what
// it does when the network is gone. The one rule that matters is that /api/ (the conversation, the
// pictures, the live stream) is never answered from a copy.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const SOURCE = readFileSync(fileURLToPath(new URL("../../src/personal_agent/channels/web/static/sw.js", import.meta.url)), "utf8");
const ORIGIN = "http://localhost:8765";

function boot({ version = "v1", stores = {} } = {}) {
  const handlers = {};
  const state = { skipped: 0, claimed: 0, fetched: [], precached: [], online: true, status: 200 };
  const caches = new Map(Object.entries(stores).map(([name, urls]) => [name, new Map(urls.map((u) => [new URL(u, ORIGIN).href, new Response("old " + u)]))]));
  const keyOf = (x) => new URL(typeof x === "string" ? x : x.url, ORIGIN).href;
  const cacheApi = {
    keys: async () => [...caches.keys()],
    delete: async (name) => caches.delete(name),
    open: async (name) => {
      if (!caches.has(name)) caches.set(name, new Map());
      const store = caches.get(name);
      return {
        addAll: async (requests) => {
          for (const request of requests) {
            state.precached.push(request);
            store.set(keyOf(request), new Response("shell " + request.url));
          }
        },
        put: async (request, response) => void store.set(keyOf(request), response),
        match: async (request) => store.get(keyOf(request))?.clone(),
        keys: async () => [...store.keys()],
      };
    },
  };
  const self = {
    location: { origin: ORIGIN },
    addEventListener: (type, fn) => (handlers[type] = fn),
    skipWaiting: async () => void (state.skipped += 1),
    clients: { claim: async () => void (state.claimed += 1) },
  };
  const net = async (request) => {
    state.fetched.push(request.url);
    if (!state.online) throw new TypeError("network down");
    return new Response("fresh " + request.url, { status: state.status });
  };
  // In a worker "/" means this origin's page; Node's Request wants a whole address.
  class WorkerRequest {
    constructor(url, init = {}) {
      this.url = new URL(url, ORIGIN).href;
      Object.assign(this, init);
    }
  }
  new Function("self", "caches", "fetch", "Request", SOURCE.replace("{{version}}", version))(self, cacheApi, net, WorkerRequest);
  return { handlers, state, caches };
}

async function lifecycle(worker, type) {
  const waiting = [];
  worker.handlers[type]({ waitUntil: (p) => waiting.push(p) });
  await Promise.all(waiting);
}

function ask(worker, url, { method = "GET", mode = "cors" } = {}) {
  const event = { request: { method, url: new URL(url, ORIGIN).href, mode }, answer: null };
  event.respondWith = (p) => (event.answer = Promise.resolve(p));
  worker.handlers.fetch(event);
  return event;
}

test("it listens for install, activate and fetch, and nothing else", () => {
  assert.deepEqual(Object.keys(boot().handlers).sort(), ["activate", "fetch", "install"]);
});

test("installing keeps the page and its files, fetched fresh, then takes over at once", async () => {
  const worker = boot({ version: "abc123" });
  await lifecycle(worker, "install");
  const kept = [...worker.caches.get("pa-shell-abc123").keys()].map((u) => u.replace(ORIGIN, ""));
  assert.ok(kept.includes("/") && kept.includes("/manifest.webmanifest"));
  assert.ok(kept.includes("/static/app.js?v=abc123") && kept.includes("/static/style.css?v=abc123"));
  assert.equal(worker.state.skipped, 1);
  // Fetched past the browser's own cache, so a new version never keeps an old file.
  assert.ok(worker.state.precached.length > 0 && worker.state.precached.every((r) => r.cache === "reload"));
});

test("nothing it keeps is under /api/", async () => {
  const worker = boot();
  await lifecycle(worker, "install");
  for (const urls of [...worker.caches.values()].map((c) => [...c.keys()])) {
    assert.ok(urls.every((u) => !new URL(u).pathname.startsWith("/api/")), urls.join(", "));
  }
});

test("a new version clears the old copies of itself, and only those", async () => {
  const worker = boot({ version: "new", stores: { "pa-shell-old": ["/"], "pa-shell-new": ["/"], "someone-elses": ["/"] } });
  await lifecycle(worker, "activate");
  assert.deepEqual([...worker.caches.keys()].sort(), ["pa-shell-new", "someone-elses"]);
  assert.equal(worker.state.claimed, 1);
});

for (const [what, url, options] of [
  ["the conversation", "/api/history?limit=40"],
  ["the live stream", "/api/stream?after=3"],
  ["a picture", "/api/media/" + "a".repeat(64)],
  ["the approvals", "/api/approvals"],
  ["its own file", "/sw.js"],
  ["a message being sent", "/static/app.js", { method: "POST" }],
  ["another site", "https://example.com/static/app.js"],
]) {
  test(`it does not answer for ${what}: the browser asks the network itself`, async () => {
    const worker = boot();
    await lifecycle(worker, "install");
    worker.state.fetched.length = 0;
    const event = ask(worker, url, options);
    assert.equal(event.answer, null);
    assert.deepEqual(worker.state.fetched, []);
  });
}

test("the page's files come from the network when it is there, and are kept for later", async () => {
  const worker = boot();
  const event = ask(worker, "/static/md.js");
  const response = await event.answer;
  assert.equal(await response.text(), "fresh " + ORIGIN + "/static/md.js");
  const cache = worker.caches.get("pa-shell-v1");
  assert.equal(await cache.get(ORIGIN + "/static/md.js").text(), "fresh " + ORIGIN + "/static/md.js");
});

test("an error page from the server is shown but never kept in place of a good copy", async () => {
  const worker = boot({ stores: { "pa-shell-v1": ["/static/md.js"] } });
  worker.state.status = 500;
  const response = await ask(worker, "/static/md.js").answer;
  assert.equal(response.status, 500);
  assert.equal(await worker.caches.get("pa-shell-v1").get(ORIGIN + "/static/md.js").text(), "old /static/md.js");
});

test("offline, a file it kept is served", async () => {
  const worker = boot({ stores: { "pa-shell-v1": ["/static/md.js"] } });
  worker.state.online = false;
  const response = await ask(worker, "/static/md.js").answer;
  assert.equal(await response.text(), "old /static/md.js");
});

test("offline, opening the page by any address gives the page, so it can say it cannot reach the agent", async () => {
  const worker = boot({ stores: { "pa-shell-v1": ["/"] } });
  worker.state.online = false;
  const response = await ask(worker, "/some/old/bookmark", { mode: "navigate" }).answer;
  assert.equal(await response.text(), "old /");
});

test("offline with nothing kept, it says so plainly", async () => {
  const worker = boot();
  worker.state.online = false;
  const response = await ask(worker, "/static/md.js").answer;
  assert.equal(response.status, 503);
});

test("offline, an api call is not answered from the cache even if a copy somehow got in", async () => {
  const worker = boot({ stores: { "pa-shell-v1": ["/api/history?limit=40", "/api/status"] } });
  worker.state.online = false;
  for (const path of ["/api/history?limit=40", "/api/status"]) {
    assert.equal(ask(worker, path).answer, null);
  }
});
