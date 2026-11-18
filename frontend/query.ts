/** Private, in-memory server state shared by the chat and collaboration pages.
 *
 * Query Core owns GET deduplication, request state and cancellation. Every completed GET is
 * immediately stale: a local cache must never stand in for a fresh authorization decision.
 * Mutation bodies (including pairing codes) never enter a query/mutation cache and are sent
 * exactly once. A failed connection does not prove a write failed on the server.
 */
import { isCancelledError, QueryClient } from "@tanstack/query-core";
export { buildTaskRequest, buildStepRequest, buildAnswerRequest } from "./contracts";

export class ApiError extends Error {
  constructor(public readonly status: number, message: string, public readonly data: unknown = null) {
    super(message);
    this.name = "ApiError";
  }
}

export interface RequestOptions {
  method?: string;
  json?: unknown;
  form?: FormData;
  signal?: AbortSignal;
}

export interface RequestActivity {
  pending: number;
  cached: number;
}

interface ScopeOptions {
  resolve?: (path: string) => string;
  fetcher?: typeof fetch;
  onAccessLost?: (status: 401 | 403) => void;
}

const interrupted = () => new DOMException("Session or space changed", "AbortError");
const unreachable = () => new ApiError(0, "Cannot reach the agent. Is the computer awake and the agent running?");

export class RequestScope {
  readonly #client = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        staleTime: 0,
        gcTime: 0,
        networkMode: "always",
        refetchOnWindowFocus: false,
        refetchOnReconnect: false,
        refetchOnMount: false,
      },
      mutations: { retry: false, gcTime: 0 },
    },
  });
  readonly #resolve: (path: string) => string;
  readonly #fetch: typeof fetch;
  readonly #onAccessLost: (status: 401 | 403) => void;
  readonly #transfers = new Set<AbortController>();
  readonly #listeners = new Set<(activity: RequestActivity) => void>();
  readonly #unsubscribe: () => void;
  #epoch = 0;
  #caller = 0;
  #identity = "unpaired";

  constructor({ resolve = path => path, fetcher = (...args) => fetch(...args), onAccessLost = () => {} }: ScopeOptions = {}) {
    this.#resolve = resolve;
    this.#fetch = fetcher;
    this.#onAccessLost = onAccessLost;
    this.#unsubscribe = this.#client.getQueryCache().subscribe(() => this.#notify());
  }

  /** Call after a verified session/space response, never with a bearer credential. */
  setContext(identity: string): void {
    if (identity === this.#identity) return;
    this.#identity = identity;
    this.clear();
  }

  /** Aborts reads, writes and downloads and discards every old private cache entry. */
  clear(): void {
    this.#epoch += 1;
    for (const controller of this.#transfers) controller.abort();
    this.#transfers.clear();
    void this.#client.cancelQueries();
    this.#client.clear();
    this.#notify();
  }

  dispose(): void {
    this.clear();
    this.#listeners.clear();
    this.#unsubscribe();
  }

  get activity(): RequestActivity {
    return {
      pending: this.#client.isFetching() + this.#transfers.size,
      cached: this.#client.getQueryCache().getAll().filter(query => query.state.data !== undefined).length,
    };
  }

  subscribe(listener: (activity: RequestActivity) => void): () => void {
    this.#listeners.add(listener);
    listener(this.activity);
    return () => this.#listeners.delete(listener);
  }

  #notify(): void {
    const activity = this.activity;
    for (const listener of this.#listeners) listener(activity);
  }

  #url(path: string): string {
    const url = this.#resolve(path);
    if (!url.startsWith("/") || url.startsWith("//") || /[\\\u0000-\u0020\u007f]/.test(url)) {
      throw new ApiError(400, "Requests must stay on this server.");
    }
    return url;
  }

  #check(epoch: number, signal: AbortSignal): void {
    if (epoch !== this.#epoch || signal.aborted) throw interrupted();
  }

  #accessLost(error: unknown, epoch: number): void {
    if (epoch !== this.#epoch || !(error instanceof ApiError) || (error.status !== 401 && error.status !== 403)) return;
    this.clear();
    // Notify the UI synchronously: another member of a Promise.all may reject with cancellation
    // first. Clearing rendered private content must not depend on which rejection wins that race.
    this.#onAccessLost(error.status);
  }

  /** No persistence plugin, automatic retries, window-focus refetch or offline replay. */
  async request<T = unknown>(path: string, options: RequestOptions = {}): Promise<T> {
    const method = (options.method ?? "GET").toUpperCase();
    const epoch = this.#epoch;
    const url = this.#url(path);
    if (options.signal?.aborted) throw interrupted();
    try {
      let data: T;
      if (method === "GET") {
        // Caller-owned cancellation must not abort another consumer of an otherwise shared GET.
        const caller = options.signal ? ++this.#caller : 0;
        data = await this.#client.fetchQuery({
          queryKey: ["private", this.#identity, epoch, url, caller],
          queryFn: ({ signal }) => this.#send<T>(url, options, epoch, signal, "json"),
        });
      } else data = await this.#direct<T>(url, { ...options, method }, epoch, "json");
      // Query state notifications and promise continuations run after the transport's own check.
      // Revocation during that gap must not return an already-fulfilled private value to the UI.
      if (epoch !== this.#epoch || options.signal?.aborted) throw interrupted();
      return data;
    } catch (error) {
      if (isCancelledError(error)) throw interrupted();
      this.#accessLost(error, epoch);
      throw error;
    } finally {
      // Even a lost write response may have changed server state. Cancel pre-write reads so they
      // cannot overwrite the caller's following refresh. Do not retry or manufacture success.
      if (method !== "GET" && epoch === this.#epoch) {
        void this.#client.cancelQueries();
        this.#client.removeQueries();
      }
    }
  }

  /** Binary artifacts remain outside Query Core's cache, including during download. */
  async blob(path: string, options: Pick<RequestOptions, "signal"> = {}): Promise<Blob> {
    const epoch = this.#epoch;
    try {
      const data = await this.#direct<Blob>(this.#url(path), options, epoch, "blob");
      if (epoch !== this.#epoch || options.signal?.aborted) throw interrupted();
      return data;
    } catch (error) {
      this.#accessLost(error, epoch);
      throw error;
    }
  }

  async #direct<T>(url: string, options: RequestOptions, epoch: number, kind: "json" | "blob"): Promise<T> {
    const controller = new AbortController();
    this.#transfers.add(controller);
    this.#notify();
    try {
      return await this.#send<T>(url, options, epoch, controller.signal, kind);
    } finally {
      this.#transfers.delete(controller);
      this.#notify();
    }
  }

  async #send<T>(url: string, options: RequestOptions, epoch: number, querySignal: AbortSignal, kind: "json" | "blob"): Promise<T> {
    const controller = new AbortController();
    const signals = options.signal ? [querySignal, options.signal] : [querySignal];
    const abort = () => controller.abort();
    for (const signal of signals) {
      if (signal.aborted) abort();
      signal.addEventListener("abort", abort, { once: true });
    }
    try {
      this.#check(epoch, controller.signal);
      const headers: Record<string, string> = { Accept: kind === "json" ? "application/json" : "*/*", "X-PA": "1" };
      let body: BodyInit | undefined;
      if (options.json !== undefined) {
        headers["Content-Type"] = "application/json";
        body = JSON.stringify(options.json);
      } else if (options.form !== undefined) body = options.form;
      let response: Response;
      try {
        response = await this.#fetch(url, {
          method: options.method ?? "GET", headers, body, signal: controller.signal,
          credentials: "same-origin", cache: "no-store",
        });
      } catch {
        this.#check(epoch, controller.signal);
        throw unreachable();
      }
      let data: unknown = null;
      let validJSON = false;
      if (kind === "blob" && response.ok) {
        try { data = await response.blob(); }
        catch {
          this.#check(epoch, controller.signal);
          throw new ApiError(0, "The download was interrupted. Please try again.");
        }
      } else if (response.status === 204 || response.status === 205) {
        validJSON = true;
      } else if (!response.headers || (response.headers.get("content-type") ?? "").toLowerCase().includes("json")) {
        try { data = await response.json(); validJSON = true; }
        catch { data = null; }
      }
      this.#check(epoch, controller.signal);
      if (!response.ok) {
        const detail = data && typeof data === "object" ? data as Record<string, unknown> : {};
        const message = typeof detail.error === "string" ? detail.error
          : typeof detail.detail === "string" ? detail.detail : `The agent answered ${response.status}.`;
        throw new ApiError(response.status, message, data);
      }
      if (kind === "json" && !validJSON) {
        throw new ApiError(0, "The agent returned an incomplete or invalid response. Refresh before trying again.");
      }
      return data as T;
    } finally {
      for (const signal of signals) signal.removeEventListener("abort", abort);
    }
  }
}
