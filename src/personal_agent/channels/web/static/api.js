// Talking to the server: requests, and the stream of what happens while the page is open.

let requestEpoch = 0;
const activeRequests = new Set();
export function invalidateRequests() {
  requestEpoch += 1;
  for (const controller of activeRequests) controller.abort();
  activeRequests.clear();
}
function epochCheck(epoch) {
  if (epoch !== requestEpoch) throw new DOMException("Session changed", "AbortError");
}

export function scopedURL(path) {
  if (typeof location === "undefined") return path;
  const space = new URLSearchParams(location.search).get("space");
  if (!space) return path;
  const url = new URL(path, location.origin);
  url.searchParams.set("space", space);
  return url.pathname + url.search;
}

export class ApiError extends Error {
  constructor(status, message, data = null) {
    super(message);
    this.status = status;
    this.data = data;
  }
}

/**
 * One request. Sends the header that only this page can add (a form on another site cannot), and
 * turns every failure into an `ApiError` whose message is fit to show: a status of 0 means the
 * server could not be reached at all.
 */
export async function api(path, { method = "GET", json, form, signal } = {}) {
  const epoch = requestEpoch;
  const controller = new AbortController();
  activeRequests.add(controller);
  const abort = () => controller.abort();
  if (signal?.aborted) controller.abort();
  signal?.addEventListener("abort", abort, {once:true});
  try {
  const headers = { Accept: "application/json", "X-PA": "1" };
  let body;
  if (json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(json);
  } else if (form) {
    body = form; // the browser sets the multipart boundary itself
  }
  let response;
  try {
    response = await fetch(scopedURL(path), { method, headers, body, signal: controller.signal, credentials: "same-origin", cache: "no-store" });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw new ApiError(0, "Cannot reach the agent. Is the computer awake and the agent running?");
  }
  let data = null;
  if ((response.headers.get("content-type") ?? "").includes("json")) {
    try {
      data = await response.json();
    } catch {
      data = null;
    }
  }
  epochCheck(epoch);
  if (!response.ok) {
    throw new ApiError(response.status, data?.error ?? `The agent answered ${response.status}.`, data);
  }
  return data;
  } finally {
    activeRequests.delete(controller);
    signal?.removeEventListener("abort", abort);
  }
}

/** Fetch an immutable result through the paired browser session, never a public bearer link. */
export async function artifactBlob(id) {
  const epoch = requestEpoch;
  if (typeof id !== "string" || !/^[0-9a-f]{32}$/.test(id)) throw new ApiError(400, "Invalid file reference.");
  let response;
  try {
    response = await fetch(scopedURL(`/api/artifacts/${id}`), {
      headers: { "X-PA": "1" }, credentials: "same-origin", cache: "no-store",
    });
  } catch {
    throw new ApiError(0, "Cannot reach the agent. Please try again when connected.");
  }
  if (!response.ok) {
    const messages = {
      401: "Pair this browser again to download files.",
      404: "This file is unavailable.",
      410: "This file has expired. Ask the agent to create it again.",
    };
    throw new ApiError(response.status, messages[response.status] ?? "The file could not be downloaded.");
  }
  try {
    const blob = await response.blob();
    epochCheck(epoch);
    return blob;
  } catch {
    throw new ApiError(0, "The download was interrupted. Please try again.");
  }
}

/**
 * The live stream. The browser's EventSource already reconnects by itself and says where it left
 * off (the server numbers turns), so this adds only what it does not do:
 *  - telling a dead session (401: stop and sign in again) from a dropped network (try again);
 *  - noticing a stream that a sleeping phone left half-open: `reconnect()` starts a fresh one.
 * Whatever is missed is replayed from the database, so a late or lost nudge costs nothing.
 */
export class Live {
  constructor({ after, onHello, onTurn, onActivity, onApprovals, onDrafts = () => {}, onSignout, onState }) {
    this.after = after;
    this.on = { onHello, onTurn, onActivity, onApprovals, onDrafts, onSignout, onState };
    this.source = null;
    this.state = "closed";
    this.retryTimer = null;
    this.failures = 0;
  }

  start() {
    this.stop();
    const url = `/api/stream?after=${encodeURIComponent(this.after())}`;
    const source = new EventSource(scopedURL(url));
    this.source = source;
    this._set("connecting");
    source.addEventListener("hello", (e) => {
      if (this.source !== source) return;
      this.failures = 0;
      this._set("open");
      this.on.onHello(JSON.parse(e.data));
    });
    for (const [event, callback] of [["turn", "onTurn"], ["activity", "onActivity"], ["approvals", "onApprovals"], ["drafts", "onDrafts"]]) {
      source.addEventListener(event, (e) => {
        if (this.source === source) this.on[callback](JSON.parse(e.data));
      });
    }
    source.addEventListener("signout", () => {
      if (this.source !== source) return;
      this.stop();
      this.on.onSignout();
    });
    source.onerror = () => {
      if (this.source !== source) return;
      this._set("down");
      if (source.readyState === EventSource.CLOSED) this._check(source);
    };
  }

  /** Close the stream and open a new one that picks up after the last turn this page has. */
  reconnect() {
    if (this.state === "closed" && !this.retryTimer) return;
    this.start();
  }

  stop() {
    clearTimeout(this.retryTimer);
    this.retryTimer = null;
    if (this.source) {
      this.source.onerror = null;
      this.source.close();
      this.source = null;
    }
    this._set("closed");
  }

  async _check(source) {
    // the browser gave up on this stream: was it the session, or only the network?
    try {
      await api("/api/session");
    } catch (error) {
      if (this.source !== source) return;
      if ([401, 403].includes(error.status)) {
        this.stop();
        this.on.onSignout();
        return;
      }
    }
    if (this.source !== source) return;
    this.failures += 1;
    const wait = Math.min(30_000, 1500 * 2 ** Math.min(this.failures, 5));
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      this.start();
    }, wait);
  }

  _set(state) {
    if (state === this.state) return;
    this.state = state;
    this.on.onState(state);
  }
}
