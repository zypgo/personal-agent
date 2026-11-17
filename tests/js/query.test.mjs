import assert from "node:assert/strict";
import { test } from "node:test";
import { ApiError, RequestScope, buildTaskRequest } from "../../src/personal_agent/channels/web/static/query.js";

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}
const response = (data, status = 200) => new Response(JSON.stringify(data), {
  status, headers: { "Content-Type": "application/json" },
});
const interrupted = error => error.name === "AbortError";
const settled = () => new Promise(setImmediate);
function scope(t, options) {
  const requests = new RequestScope(options);
  t.after(() => requests.dispose());
  return requests;
}

test("concurrent reads share one request, but completed private data never replaces a fresh authorization check", async t => {
  const first = deferred();
  const calls = [];
  const requests = scope(t, { fetcher: async (url, options) => {
    calls.push({ url, options });
    return calls.length === 1 ? await first.promise : response({ revision: 2 });
  } });
  const a = requests.request("/api/v3/tasks");
  const b = requests.request("/api/v3/tasks");
  assert.equal(calls.length, 1);
  assert.equal(requests.activity.pending, 1);
  first.resolve(response({ revision: 1 }));
  assert.deepEqual(await Promise.all([a, b]), [{ revision: 1 }, { revision: 1 }]);
  assert.deepEqual(await requests.request("/api/v3/tasks"), { revision: 2 });
  assert.equal(calls.length, 2);
  assert.equal(calls[0].options.credentials, "same-origin");
  assert.equal(calls[0].options.cache, "no-store");
  assert.equal(calls[0].options.headers["X-PA"], "1");
  requests.clear();
  assert.deepEqual(requests.activity, { pending: 0, cached: 0 });
});

test("switching identity or space cancels an old read even when the transport ignores abort", async t => {
  const old = deferred();
  let signal;
  const requests = scope(t, { fetcher: (_url, options) => { signal = options.signal; return old.promise; } });
  requests.setContext("alice:project:member");
  const rejected = assert.rejects(requests.request("/api/v3/tasks"), interrupted);
  requests.setContext("alice:personal:owner");
  assert.equal(signal.aborted, true);
  assert.deepEqual(requests.activity, { pending: 0, cached: 0 });
  old.resolve(response({ secret: "revoked-space" }));
  await rejected;
  await settled();
  assert.equal(requests.activity.cached, 0);
});

test("logout also blocks a response whose JSON body completes after transport cancellation", async t => {
  const body = deferred();
  const requests = scope(t, { fetcher: async () => ({
    ok: true, status: 200, headers: new Headers({ "content-type": "application/json" }), json: () => body.promise,
  }) });
  const rejected = assert.rejects(requests.request("/api/history"), interrupted);
  await settled();
  requests.clear();
  body.resolve({ messages: ["private history"] });
  await rejected;
  await settled();
  assert.equal(requests.activity.cached, 0);
});

test("revocation between cache settlement and consumer delivery cannot return private data", async t => {
  const requests = scope(t, { fetcher: async () => response({ secret: "do not render" }) });
  requests.subscribe(activity => { if (activity.cached) requests.clear(); });
  await assert.rejects(requests.request("/api/history"), interrupted);
  assert.deepEqual(requests.activity, { pending: 0, cached: 0 });
});

test("failed writes are sent once, never cached or replayed, and invalidate older reads", async t => {
  const old = deferred();
  const writes = [];
  const requests = scope(t, { fetcher: async (url, options) => {
    if (options.method === "POST") { writes.push({ url, body: options.body }); throw new Error("response lost"); }
    return await old.promise;
  } });
  const rejectedRead = assert.rejects(requests.request("/api/v3/tasks"), interrupted);
  await assert.rejects(requests.request("/api/v3/tasks", { method: "POST", json: { client_request_id: "stable-id", code: "private-code" } }), error => error instanceof ApiError && error.status === 0);
  old.resolve(response({ tasks: ["stale"] }));
  await rejectedRead;
  await settled();
  assert.equal(writes.length, 1);
  assert.deepEqual(JSON.parse(writes[0].body), { client_request_id: "stable-id", code: "private-code" });
  assert.deepEqual(requests.activity, { pending: 0, cached: 0 });
});

test("a forbidden read synchronously clears access once, including deduplicated and parallel reads", async t => {
  const forbidden = deferred(), pending = deferred(), recovery = deferred();
  const events = [];
  let recovered;
  const requests = scope(t, {
    fetcher: url => url === "/forbidden" ? forbidden.promise : url === "/me" ? recovery.promise : pending.promise,
    onAccessLost: status => {
      events.push({ status, activity: requests.activity });
      // Recovery started here must survive another consumer of the same forbidden request.
      recovered = requests.request("/me");
    },
  });
  const reads = Promise.allSettled([requests.request("/forbidden"), requests.request("/forbidden"), requests.request("/parallel")]);
  forbidden.resolve(response({ error: "Membership revoked" }, 403));
  const results = await reads;
  assert.ok(results.every(result => result.status === "rejected"));
  assert.deepEqual(events, [{ status: 403, activity: { pending: 0, cached: 0 } }]);
  pending.resolve(response({ secret: "old-space" }));
  recovery.resolve(response({ principal: "alice" }));
  assert.deepEqual(await recovered, { principal: "alice" });
});

test("a revoked session clears a rendered-view callback before its request rejects", async t => {
  let rendered = "private text";
  const requests = scope(t, {
    fetcher: async () => response({ error: "Sign in again" }, 401),
    onAccessLost: status => { assert.equal(status, 401); rendered = ""; },
  });
  await assert.rejects(requests.request("/api/session"), error => error.status === 401 && rendered === "");
  assert.equal(requests.activity.cached, 0);
});

test("one caller's abort cannot cancel another caller's read of the same route", async t => {
  const responses = [], signals = [];
  const requests = scope(t, { fetcher: (_url, { signal }) => new Promise((resolve, reject) => {
    responses.push(resolve); signals.push(signal);
    signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true });
  }) });
  const controller = new AbortController();
  const rejected = assert.rejects(requests.request("/api/history", { signal: controller.signal }), interrupted);
  const independent = requests.request("/api/history");
  controller.abort();
  assert.equal(signals[0].aborted, true);
  assert.equal(signals[1].aborted, false);
  responses[1](response({ history: [] }));
  assert.deepEqual(await independent, { history: [] });
  await rejected;
});

test("downloads never enter the query cache and are discarded on space change", async t => {
  const contents = deferred();
  let signal;
  const requests = scope(t, { fetcher: async (_url, options) => {
    signal = options.signal;
    return { ok: true, blob: () => contents.promise };
  } });
  const rejected = assert.rejects(requests.blob("/api/artifacts/id"), interrupted);
  await settled();
  assert.equal(requests.activity.pending, 1);
  assert.equal(requests.activity.cached, 0);
  requests.clear();
  contents.resolve(new Blob(["private file"]));
  await rejected;
  assert.equal(signal.aborted, true);
  assert.deepEqual(requests.activity, { pending: 0, cached: 0 });
});

test("cross-origin paths and already-aborted requests never reach the network", async t => {
  let calls = 0;
  const requests = scope(t, { fetcher: async () => { calls++; return response({}); } });
  for (const path of ["https://other.invalid/", "//other.invalid/", "/\\other.invalid/", "javascript:alert(1)", "/api/\nsecret"]) {
    await assert.rejects(requests.request(path), error => error.status === 400);
  }
  const controller = new AbortController(); controller.abort();
  await assert.rejects(requests.request("/api/v3/tasks", { signal: controller.signal }), interrupted);
  await assert.rejects(requests.blob("/api/artifacts/id", { signal: controller.signal }), interrupted);
  assert.equal(calls, 0);
});

test("typed task forms preserve the caller's idempotency key and explicit command", () => {
  const fields = { title: "  Report  ", command: "printf '%s' '$literal'", input_paths: "in.txt\r\n data.csv \n", output_paths: "report.md", timeout_s: "60" };
  const task = buildTaskRequest(fields, "same-user-retry-id");
  assert.deepEqual(task, { title: "Report", command: fields.command, input_paths: ["in.txt", "data.csv"], output_paths: ["report.md"], timeout_s: 60, client_request_id: "same-user-retry-id" });
  assert.deepEqual(buildTaskRequest(fields, "same-user-retry-id"), task);
});
