// Browser requests share one typed, in-memory Query Core adapter. No private state is persisted.
import { ApiError, RequestScope } from "./query.js";
export { ApiError };

let accessLost = () => {};
const requests = new RequestScope({ resolve: scopedURL, onAccessLost: status => accessLost(status) });
export function invalidateRequests() { requests.clear(); }
export function setRequestContext(principal) { requests.setContext(String(principal ?? "owner")); }
export function onAccessLost(callback) { accessLost = callback; }

export function scopedURL(path) {
  if (typeof location === "undefined") return path;
  const space = new URLSearchParams(location.search).get("space");
  if (!space) return path;
  const url = new URL(path, location.origin);
  url.searchParams.set("space", space);
  return url.pathname + url.search;
}

/** GET requests share in-flight work. Writes are sent once and never enter a cache. */
export function api(path, options = {}) { return requests.request(path, options); }

/** Immutable downloads are authenticated, cancellable and kept outside the query cache. */
export async function artifactBlob(id) {
  if (typeof id !== "string" || !/^[0-9a-f]{32}$/.test(id)) throw new ApiError(400, "Invalid file reference.");
  try { return await requests.blob(`/api/artifacts/${id}`); }
  catch (error) {
    if (error?.name === "AbortError" || error.status === 0) throw error;
    const messages = {
      401: "Pair this browser again to download files.",
      404: "This file is unavailable.",
      410: "This file has expired. Ask the agent to create it again.",
    };
    throw new ApiError(error.status || 0, messages[error.status] ?? "The file could not be downloaded.");
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
