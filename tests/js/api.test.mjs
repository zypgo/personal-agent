import assert from "node:assert/strict";
import { test } from "node:test";
import { ApiError, artifactBlob, Live } from "../../src/personal_agent/channels/web/static/api.js";

test("artifact downloads use the authenticated same-origin route and never cache private files", async (t) => {
  const id = "a".repeat(32);
  t.mock.method(globalThis, "fetch", async (path, options) => {
    assert.equal(path, `/api/artifacts/${id}`);
    assert.equal(options.credentials, "same-origin");
    assert.equal(options.cache, "no-store");
    assert.equal(options.headers["X-PA"], "1");
    return new Response("the file");
  });
  assert.equal(await (await artifactBlob(id)).text(), "the file");
});

test("a malformed artifact reference never becomes a request", async (t) => {
  const network = t.mock.method(globalThis, "fetch", () => { throw Error("must not request"); });
  for (const id of ["../session", "https://example.com", null, "a".repeat(64)]) {
    await assert.rejects(artifactBlob(id), (e) => e instanceof ApiError && e.status === 400);
  }
  assert.equal(network.mock.callCount(), 0);
});

test("expired, revoked and interrupted downloads explain how to recover", async (t) => {
  let status = 410;
  t.mock.method(globalThis, "fetch", async () => new Response("private server details", { status }));
  await assert.rejects(artifactBlob("a".repeat(32)), /expired/);
  status = 401;
  await assert.rejects(artifactBlob("a".repeat(32)), /Pair this browser/);
  status = 404;
  await assert.rejects(artifactBlob("a".repeat(32)), /unavailable/);
  status = 500;
  await assert.rejects(artifactBlob("a".repeat(32)), /could not be downloaded/);
});

test("the stream dispatches preview snapshots without advancing the history cursor", (t) => {
  const previous = globalThis.EventSource;
  class Source {
    constructor(url) { this.url = url; this.listeners = new Map(); }
    addEventListener(name, callback) { this.listeners.set(name, callback); }
    close() { this.closed = true; }
  }
  globalThis.EventSource = Source;
  t.after(() => { if (previous === undefined) delete globalThis.EventSource; else globalThis.EventSource = previous; });
  const seen = [];
  const live = new Live({
    after: () => 7, onHello: () => {}, onTurn: () => { throw Error("not a durable turn"); },
    onDrafts: (value) => seen.push(value), onState: () => {},
  });
  live.start();
  const snapshot = { drafts: [{ run_id: "r1", sequence: 3, text: "First" }] };
  live.source.listeners.get("drafts")({ data: JSON.stringify(snapshot) });
  assert.deepEqual(seen, [snapshot]);
  assert.equal(live.source.url, "/api/stream?after=7");
  const first = live.source;
  live.reconnect();
  assert.equal(first.closed, true);
  assert.equal(live.source.url, "/api/stream?after=7");
  first.listeners.get("drafts")({ data: JSON.stringify({ drafts: [] }) });
  assert.equal(seen.length, 1); // a queued callback from the replaced connection is stale
  live.stop();
});

test("a delayed authentication check cannot reopen a stream after logout", async (t) => {
  let resolve;
  t.mock.method(globalThis, "fetch", () => new Promise((r) => { resolve = r; }));
  const source = { close() {}, onerror: null };
  const live = new Live({ after: () => 0, onState: () => {}, onSignout: () => { throw Error("stale signout"); } });
  live.source = source;
  live.state = "open";
  const check = live._check(source);
  live.stop();
  resolve(new Response("{}", { headers: { "content-type": "application/json" } }));
  await check;
  assert.equal(live.source, null);
  assert.equal(live.retryTimer, null);
  assert.equal(live.state, "closed");
});
