import assert from "node:assert/strict";
import { test } from "node:test";
import { ApiError, artifactBlob, scopedArtifactBlob, agentSetupBlob, Live } from "../../src/personal_agent/channels/web/static/api.js";

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

test("agent result downloads retain the current space and authenticated no-store transport", async (t) => {
  const previous = globalThis.location;
  globalThis.location = { origin: "https://assistant.example", search: "?space=project-a" };
  t.after(() => { if (previous === undefined) delete globalThis.location; else globalThis.location = previous; });
  const path = `/api/v4/artifacts/${"a".repeat(32)}`;
  t.mock.method(globalThis, "fetch", async (url, options) => {
    assert.equal(url, `${path}?space=project-a`);
    assert.equal(options.credentials, "same-origin");
    assert.equal(options.cache, "no-store");
    assert.equal(options.headers["X-PA"], "1");
    return new Response("agent result");
  });
  assert.equal(await (await scopedArtifactBlob(path)).text(), "agent result");
});

test("agent result references cannot become arbitrary or encoded-path requests", async (t) => {
  const network = t.mock.method(globalThis, "fetch", () => { throw Error("must not request"); });
  for (const path of [null, "/api/session", "https://assistant.example/api/v4/artifacts/" + "a".repeat(32),
    "/api/v4/artifacts/../session", "/api/v4/artifacts/%2e%2e", "/api/v4/artifacts/%2fapi%2fsession",
    `/api/v4/artifacts/${"a".repeat(32)}?space=someone-else`, `/api/v4/artifacts/${"a".repeat(32)}#ignored`]) {
    await assert.rejects(scopedArtifactBlob(path), (e) => e instanceof ApiError && e.status === 400);
  }
  assert.equal(network.mock.callCount(), 0);
});

test("connection files use fixed authenticated routes and the selected space", async (t) => {
  const previous = globalThis.location;
  globalThis.location = { origin: "https://assistant.example", search: "?space=project-a" };
  t.after(() => { if (previous === undefined) delete globalThis.location; else globalThis.location = previous; });
  const seen = [];
  t.mock.method(globalThis, "fetch", async (path, options) => {
    seen.push(path);
    assert.equal(options.credentials, "same-origin");
    assert.equal(options.cache, "no-store");
    assert.equal(options.headers["X-PA"], "1");
    return new Response("connection setup");
  });
  assert.equal(await (await agentSetupBlob("ag_example", "openapi")).text(), "connection setup");
  await agentSetupBlob("ag_example", "bridge");
  assert.deepEqual(seen, ["/api/v4/agents/ag_example/openapi.json?space=project-a", "/api/v4/bridge.py?space=project-a"]);
});

test("connection files reject arbitrary URLs, path components and unknown kinds", async (t) => {
  const network = t.mock.method(globalThis, "fetch", () => { throw Error("must not request"); });
  for (const id of [null, "..", "%2e%2e", "a/../b", "https://elsewhere.invalid", "ag_x?space=other", "ag_x#other"]) {
    await assert.rejects(agentSetupBlob(id, "openapi"), (e) => e instanceof ApiError && e.status === 400);
  }
  for (const kind of [null, "https://elsewhere.invalid", "../session", "token"]) {
    await assert.rejects(agentSetupBlob("ag_example", kind), (e) => e instanceof ApiError && e.status === 400);
  }
  assert.equal(network.mock.callCount(), 0);
});

test("expired, revoked and interrupted downloads explain how to recover", async (t) => {
  let status = 410;
  t.mock.method(globalThis, "fetch", async () => new Response("private server details", { status }));
  await assert.rejects(artifactBlob("a".repeat(32)), /已过期/);
  status = 401;
  await assert.rejects(artifactBlob("a".repeat(32)), /重新连接此浏览器/);
  status = 404;
  await assert.rejects(artifactBlob("a".repeat(32)), /不可用/);
  status = 500;
  await assert.rejects(artifactBlob("a".repeat(32)), /下载失败/);
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
  const changed = [];
  const live = new Live({
    after: () => 7, onHello: () => {}, onTurn: () => { throw Error("not a durable turn"); },
    onDrafts: (value) => seen.push(value), onState: () => {},
    onAssistantChanged: (value) => changed.push(value),
  });
  live.start();
  const snapshot = { drafts: [{ run_id: "r1", sequence: 3, text: "First" }] };
  live.source.listeners.get("drafts")({ data: JSON.stringify(snapshot) });
  assert.deepEqual(seen, [snapshot]);
  live.source.listeners.get("assistant_changed")({ data: "{}" });
  assert.deepEqual(changed, [{}]);
  assert.equal(live.source.url, "/api/stream?after=7");
  const first = live.source;
  live.reconnect();
  assert.equal(first.closed, true);
  assert.equal(live.source.url, "/api/stream?after=7");
  first.listeners.get("drafts")({ data: JSON.stringify({ drafts: [] }) });
  first.listeners.get("assistant_changed")({ data: "{}" });
  assert.equal(changed.length, 1);
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
