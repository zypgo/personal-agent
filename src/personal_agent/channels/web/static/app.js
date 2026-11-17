// The page. It draws what the server's database says and sends what the owner types; it keeps
// nothing of its own that the server does not also have, so closing it, losing the connection or
// opening it on another phone costs nothing.
//
// Everything is built with DOM calls and `textContent`. There is no innerHTML anywhere in this
// file, and none may be added: the page's policy forbids inline code, and the answers it shows can
// repeat text that someone else wrote.

import { Live, api, artifactBlob, scopedURL, invalidateRequests } from "./api.js";
import { activityLabel, connectionLabel } from "./activity.js";
import { audioDuration } from "./audio-metadata.js";
import {
  clockTime,
  commandHints,
  dayKey,
  dayLabel,
  durationLabel,
  expiresIn,
  newClientId,
  parsePairing,
} from "./format.js";
import { canRecord, record, shrinkImage } from "./media.js";
import { renderMarkdown } from "./md.js";
import { Store } from "./store.js";

const PAGE_VERSION = new URL(import.meta.url).searchParams.get("v") ?? "";
const STICK_PX = 140;
const KEPT_NOTHING =
  "Paired, but this browser did not keep the sign-in. Check that cookies are allowed for this site " +
  "(a private window may refuse them), and that the address is the one the agent expects.";
const THEMES = ["auto", "light", "dark"];

const root = document.getElementById("app");
const store = new Store();
let session = null;
let live = null;
let ui = null; // the chat's elements, once it is built
let unread = 0;
let pinned = true; // the log was at its end the last time it moved
const rendered = new Map(); // key -> { node, signature }
const nowSeconds = () => Date.now() / 1000;

// ---- small helpers --------------------------------------------------------------------------
function h(tag, props = {}, ...kids) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props ?? {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    node.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return node;
}

const SVG = "http://www.w3.org/2000/svg";
const ICONS = {
  send: "M12 19V5M5 12l7-7 7 7",
  clip: "M21 12.5l-8.6 8.6a5.5 5.5 0 0 1-7.8-7.8l9-9a3.7 3.7 0 0 1 5.2 5.2l-9 9a1.8 1.8 0 0 1-2.6-2.6l8.4-8.4",
  mic: "M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3zM5 11a7 7 0 0 0 14 0M12 18v3",
  stop: "M7 7h10v10H7z",
  close: "M6 6l12 12M18 6L6 18",
  more: "M5 12h.01M12 12h.01M19 12h.01",
  down: "M12 5v14M5 12l7 7 7-7",
  check: "M5 12.5l4.5 4.5L19 7",
};

function icon(name) {
  const svg = document.createElementNS(SVG, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("class", `icon icon-${name}`);
  const path = document.createElementNS(SVG, "path");
  path.setAttribute("d", ICONS[name]);
  svg.append(path);
  return svg;
}

function iconButton(name, label, onclick, extra = "") {
  return h("button", { type: "button", class: `icon-button ${extra}`.trim(), "aria-label": label, title: label, onclick }, icon(name));
}

function toast(message, { error = false } = {}) {
  if (!ui) return;
  const node = h("div", { class: `toast${error ? " error" : ""}`, role: error ? "alert" : "status", text: message });
  ui.toasts.append(node);
  setTimeout(() => node.remove(), error ? 7000 : 3500);
}

function reducedMotion() {
  return matchMedia("(prefers-reduced-motion: reduce)").matches;
}

// ---- theme ----------------------------------------------------------------------------------
function theme() {
  const stored = localStorage.getItem("pa-theme");
  return THEMES.includes(stored) ? stored : "auto";
}

function applyTheme(value) {
  document.documentElement.dataset.theme = value;
  const dark = value === "dark" || (value === "auto" && matchMedia("(prefers-color-scheme: dark)").matches);
  for (const meta of document.querySelectorAll('meta[name="theme-color"]')) {
    if (!meta.dataset.original) meta.dataset.original = meta.content;
    meta.content = value === "auto" ? meta.dataset.original : dark ? "#14110d" : "#f1e9d6";
  }
}

// ---- boot -----------------------------------------------------------------------------------
async function main() {
  applyTheme(theme());
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => applyTheme(theme()));
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js", { updateViaCache: "none" }).catch(() => {});
  }
  await start();
}

async function start(notice = "", { paired = false } = {}) {
  const code = takePairingCode();
  let problem = notice;
  if (code) {
    try {
      await api("/api/pair", { method: "POST", json: { code } });
      paired = true;
    } catch (error) {
      problem = error.message;
    }
  }
  try {
    session = await api("/api/session");
  } catch (error) {
    if (error.status !== 401) return showOffline(error.message);
    return showPairing(paired ? KEPT_NOTHING : problem);
  }
  document.title = session.agent;
  await showChat(problem);
}

/** The code in the address (`#pair=...`), taken out of the address bar and history at once. */
function takePairingCode() {
  const code = parsePairing(location.hash);
  if (location.hash) history.replaceState(null, "", location.pathname + location.search);
  return code;
}

// ---- pairing --------------------------------------------------------------------------------
function showPairing(problem = "") {
  teardownChat();
  const input = h("input", {
    type: "text",
    name: "code",
    autocomplete: "off",
    autocapitalize: "off",
    spellcheck: "false",
    placeholder: "Paste the link or the code",
    "aria-label": "Pairing link or code",
  });
  const error = h("p", { class: "form-error", role: "alert", text: problem });
  const button = h("button", { type: "submit", class: "primary", text: "Pair this browser" });
  const form = h("form", { class: "pair-form" }, input, button);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const code = parsePairing(input.value);
    if (!code) {
      error.textContent = "That does not look like a pairing link or code. Copy the whole link.";
      return;
    }
    button.disabled = true;
    error.textContent = "";
    try {
      await api("/api/pair", { method: "POST", json: { code } });
      await start("", { paired: true });
    } catch (failure) {
      error.textContent = failure.message;
      button.disabled = false;
    }
  });
  root.replaceChildren(
    h(
      "main",
      { class: "pair" },
      h("div", { class: "pair-mark", "aria-hidden": "true" }, h("span", { class: "lamp" })),
      h("h1", { text: document.title || "Agent" }),
      h("p", { class: "lede", text: "This browser is not paired with your agent yet." }),
      h(
        "ol",
        { class: "steps" },
        h("li", {}, "On the computer where the agent runs, open a terminal and type ", h("code", { text: "pa web pair" }), "."),
        h("li", {}, "Open the link it prints in this browser, or paste the link (or just the code) here:"),
      ),
      form,
      error,
      h(
        "p",
        { class: "fine-print" },
        "The link works once and for a few minutes. On an iPhone, open this page from the Home Screen icon first, then paste: the icon and Safari do not share a sign-in.",
      ),
    ),
  );
  if (!problem) input.focus({ preventScroll: true });
}

function showOffline(message) {
  teardownChat();
  root.replaceChildren(
    h(
      "main",
      { class: "pair" },
      h("div", { class: "pair-mark offline", "aria-hidden": "true" }, h("span", { class: "lamp" })),
      h("h1", { text: document.title || "Agent" }),
      h("p", { class: "lede", text: message }),
      h("button", { type: "button", class: "primary", text: "Try again", onclick: () => start() }),
    ),
  );
}

function signedOut() {
  teardownChat();
  showPairing("You were signed out. Pair this browser again to continue.");
}

// ---- the chat -------------------------------------------------------------------------------
function teardownChat() {
  invalidateRequests();
  for (const entry of store.pending) releasePreviews(entry.files);
  store.principalId = null;
  if (live) live.stop();
  live = null;
  ui = null;
  rendered.clear();
  store.turns.clear();
  store.order = [];
  store.pending = [];
  store.approvals.clear();
  store.drafts.clear();
  store.completedRuns.clear();
  store.more = false;
  setBadge(0);
  unread = 0;
}

async function showChat(notice) {
  store.principalId = session.principal_id;
  buildChat();
  if (notice) toast(notice, { error: true });
  try {
    const page = await api("/api/history?limit=40");
    store.addTurns(page.turns);
    store.more = page.more;
  } catch (error) {
    if (error?.name === "AbortError") return;
    if ([401, 403].includes(error.status)) return signedOut();
    toast(error.message, { error: true });
  }
  renderLog();
  scrollToBottom(false);
  live = new Live({
    after: () => store.lastId,
    onHello: onHello,
    onTurn: onTurn,
    onActivity: (data) => {
      store.setWorking(data.working, data.tool);
      renderStatus();
    },
    onApprovals: (data) => {
      store.setApprovals(data.approvals);
      renderLog();
      renderStatus();
    },
    onDrafts: (data) => {
      const stick = nearBottom();
      store.replaceDrafts(data.drafts);
      renderLog();
      if (stick) scrollToBottom(false);
    },
    onSignout: signedOut,
    onState: onStreamState,
  });
  live.start();
  if (PAGE_VERSION && session.version !== PAGE_VERSION) showUpdate();
}

function buildChat() {
  const name = h("h1", { text: session.agent });
  const lamp = h("span", { class: "lamp idle", "aria-hidden": "true" });
  const state = h("span", { class: "state", "aria-live": "polite" });
  const waiting = h("button", { type: "button", class: "waiting", hidden: true, onclick: showFirstWaiting });
  const menu = iconButton("more", "Menu", openSettings);
  const banner = h("div", { class: "banner", role: "status", hidden: true });
  const update = h("button", { type: "button", class: "update", hidden: true, text: "A new version is ready. Tap to reload.", onclick: () => location.reload() });
  const loadMore = h("button", { type: "button", class: "earlier", hidden: true, text: "Earlier messages", onclick: loadEarlier });
  const working = h("div", { class: "working", hidden: true, "aria-hidden": "true" }, h("span", { class: "pen" }), h("span", { class: "working-text" }));
  const log = h("main", { class: "log", id: "log", role: "log", "aria-live": "polite", "aria-label": "Conversation", tabindex: "-1" }, loadMore, working);
  const jump = h("button", { type: "button", class: "jump", hidden: true, onclick: () => scrollToBottom(true) }, icon("down"), h("span", { class: "jump-count" }));
  const toasts = h("div", { class: "toasts" });
  ui = { name, lamp, state, waiting, banner, update, log, loadMore, working, jump, toasts, composer: buildComposer() };
  pinned = true;
  ui.log.addEventListener("scroll", onScroll, { passive: true });
  ui.log.addEventListener("click", onLogClick);
  // A picture makes its message taller once it has loaded: if the log was at its end, stay there.
  ui.log.addEventListener("load", () => pinned && scrollToBottom(false), true);
  // So does anything that makes the log itself shorter (a banner, the phone's keyboard).
  if (typeof ResizeObserver === "function") new ResizeObserver(() => pinned && scrollToBottom(false)).observe(ui.log);
  root.replaceChildren(
    h(
      "div",
      { class: "chat" },
      h("header", { class: "bar" }, h("div", { class: "who" }, lamp, name, state), h("a", { href: scopedURL("/workspace"), text: "工作台", class: "workspace-link" }), waiting, menu),
      banner,
      update,
      h("div", { class: "stage" }, log, jump),
      toasts,
      ui.composer.element,
    ),
  );
  ui.composer.refresh(); // now that it is on the page it can be measured
  renderStatus();
  watchViewport();
}

// ---- the stream's events ----------------------------------------------------------------------
function onHello(data) {
  store.setWorking(data.working);
  store.setApprovals(data.approvals ?? []);
  store.replaceDrafts(data.drafts);
  renderLog();
  renderStatus();
}

function onTurn(turn) {
  store.matchIncoming(turn);
  const fresh = store.addTurns([turn]);
  if (!fresh.length) return;
  const mine = turn.direction === "in" && turn.channel === "web";
  const stick = mine || nearBottom();
  renderLog();
  if (stick) scrollToBottom(!document.hidden);
  else bumpJump();
  if (turn.direction === "out" && document.hidden) {
    unread += 1;
    setBadge(unread);
  }
}

let bannerTimer = null;
function onStreamState(state) {
  if (!ui) return;
  renderStatus();
  clearTimeout(bannerTimer);
  if (state === "open") {
    ui.banner.hidden = true;
  } else if (state === "down") {
    bannerTimer = setTimeout(() => {
      if (!ui) return;
      ui.banner.textContent = navigator.onLine === false ? "You are offline. Waiting for a connection…" : "Reconnecting to your agent…";
      ui.banner.hidden = false;
    }, 2500);
  }
}

function renderStatus() {
  if (!ui) return;
  const offline = Boolean(live) && live.state === "down";
  ui.lamp.className = `lamp ${offline ? "down" : store.working ? "on" : "idle"}`;
  ui.state.textContent = offline ? "reconnecting…" : store.working ? (store.tool ? `working · ${store.tool}` : "working") : "idle";
  const appearing = ui.working.hidden && store.working;
  const stick = appearing && nearBottom(); // the indicator is the newest line: keep it in view
  ui.working.hidden = !store.working;
  ui.working.querySelector(".working-text").textContent = store.tool ? `using ${store.tool}` : "working on it";
  if (stick) scrollToBottom(!document.hidden);
  const waiting = store.waiting;
  ui.waiting.hidden = waiting.length === 0;
  ui.waiting.textContent = waiting.length === 1 ? "1 waiting for you" : `${waiting.length} waiting for you`;
}

function showUpdate() {
  ui.update.hidden = false;
}

// ---- the conversation on screen ---------------------------------------------------------------
function nearBottom() {
  const log = ui.log;
  return log.scrollHeight - log.scrollTop - log.clientHeight < STICK_PX;
}

function scrollToBottom(smooth) {
  if (!ui) return;
  const log = ui.log;
  log.scrollTo({ top: log.scrollHeight, behavior: smooth && !reducedMotion() ? "smooth" : "auto" });
  ui.jump.hidden = true;
  ui.jump.dataset.count = "0";
}

function bumpJump() {
  const count = Number(ui.jump.dataset.count ?? 0) + 1;
  ui.jump.dataset.count = String(count);
  ui.jump.querySelector(".jump-count").textContent = count > 1 ? `${count} new` : "New";
  ui.jump.hidden = false;
}

function onScroll() {
  pinned = nearBottom();
  if (pinned) {
    ui.jump.hidden = true;
    ui.jump.dataset.count = "0";
  }
}

async function loadEarlier() {
  const first = store.firstId;
  if (!first) return;
  ui.loadMore.disabled = true;
  try {
    const page = await api(`/api/history?limit=40&before=${first}`);
    store.more = page.more;
    const before = ui.log.scrollHeight;
    const top = ui.log.scrollTop;
    store.addTurns(page.turns);
    renderLog();
    ui.log.scrollTop = top + (ui.log.scrollHeight - before); // keep what the owner was reading in place
  } catch (error) {
    if (error?.name === "AbortError") return;
    if ([401, 403].includes(error.status)) return signedOut();
    toast(error.message, { error: true });
  } finally {
    if (ui) ui.loadMore.disabled = false;
  }
}

function renderLog() {
  if (!ui) return;
  const wanted = [];
  const seen = new Set();
  let lastDay = null;
  let previous = null;
  for (const entry of store.timeline) {
    const at = entry.turn ? entry.turn.at : entry.pending.at;
    const day = dayKey(at);
    if (day !== lastDay) {
      wanted.push(cached(`d${day}`, day, () => daySeparator(at)));
      seen.add(`d${day}`);
      lastDay = day;
      previous = null;
    }
    const node = entry.turn
      ? cached(entry.key, turnSignature(entry.turn, previous), () => turnNode(entry.turn, previous))
      : cached(entry.key, pendingSignature(entry.pending), () => pendingNode(entry.pending));
    wanted.push(node);
    seen.add(entry.key);
    previous = entry.turn ?? null;
  }
  for (const draft of store.drafts.values()) {
    if (!draft.text) continue;
    const key = `draft:${draft.run_id}`;
    wanted.push(cached(key, `${draft.sequence}|${draft.text}`, () => draftNode(draft)));
    seen.add(key);
  }
  let ref = ui.loadMore.nextSibling;
  for (const node of wanted) {
    if (node === ref) ref = ref.nextSibling;
    else ui.log.insertBefore(node, ref);
  }
  while (ref && ref !== ui.working) {
    const next = ref.nextSibling;
    ref.remove();
    ref = next;
  }
  for (const key of rendered.keys()) if (!seen.has(key)) rendered.delete(key);
  ui.loadMore.hidden = !store.more;
}

function cached(key, signature, build) {
  const known = rendered.get(key);
  if (known && known.signature === signature) return known.node;
  const node = build();
  rendered.set(key, { node, signature });
  return node;
}

function daySeparator(at) {
  return h("div", { class: "day", role: "separator" }, h("span", { text: dayLabel(at, nowSeconds()) }));
}

function draftNode(draft) {
  return h("article", { class: "turn agent streaming", "aria-busy": "true" },
    h("div", { class: "speaker", text: session.agent }),
    h("div", { class: "body" }, h("p", { class: "words", text: draft.text })),
    h("div", { class: "meta" }, h("span", { class: "tag", text: draft.truncated ? "Writing… full reply follows." : "Writing…" })),
  );
}

const approvalState = (turn) => (turn.approvals ?? []).map((a) => `${a.id}:${store.approval(a).status}`).join(",");
const turnSignature = (turn, previous) => `${turn.id}|${previous?.direction ?? "-"}|${approvalState(turn)}`;
const pendingSignature = (p) => `${p.state}|${p.error ?? ""}`;

function turnNode(turn, previous) {
  const mine = turn.direction === "in";
  const node = h("article", { class: `turn ${mine ? "mine" : "agent"}`, dataset: { id: String(turn.id), kind: turn.kind } });
  if (!mine && (!previous || previous.direction === "in")) node.append(h("div", { class: "speaker", text: session.agent }));
  const body = h("div", { class: "body" });
  const pictures = (turn.meta?.attachments ?? []).filter((a) => a.kind === "image" && /^[0-9a-f]{64}$/.test(a.id));
  if (pictures.length) body.append(h("div", { class: `pictures n${Math.min(pictures.length, 4)}` }, pictures.map((a) => pictureNode(a.id))));
  if (mine) {
    if (turn.text) body.append(h("p", { class: turn.text.startsWith("/") ? "words command" : "words", text: turn.text }));
  } else {
    body.append(renderMarkdown(document, turn.text));
  }
  if (!mine && Array.isArray(turn.meta?.artifacts)) {
    for (const file of turn.meta.artifacts.slice(0, 10)) {
      if (file && /^[0-9a-f]{32}$/.test(file.id) && typeof file.name === "string") body.append(artifactNode(file));
    }
  }
  for (const approval of turn.approvals ?? []) body.append(approvalNode(approval));
  node.append(body, metaLine(turn));
  return node;
}

function artifactNode(file) {
  const name = file.name.slice(0, 240);
  const expired = Number.isFinite(file.expires_at) && file.expires_at <= nowSeconds();
  const size = Number.isSafeInteger(file.size) && file.size >= 0
    ? (file.size < 1024 ? `${file.size} B` : `${Math.ceil(file.size / 1024)} KB`)
    : "File";
  const status = h("span", { class: "artifact-status", role: "status", text: expired ? "Expired · ask for a new file" : size });
  const button = h("button", {
    type: "button", class: "artifact-download", disabled: expired, text: `Download ${name}`,
    onclick: async () => {
      button.disabled = true;
      status.textContent = "Downloading…";
      let url;
      try {
        const blob = await artifactBlob(file.id);
        url = URL.createObjectURL(blob);
        const link = h("a", { href: url, download: name });
        document.body.append(link);
        link.click();
        link.remove();
        status.textContent = size;
      } catch (error) {
        status.textContent = error.message;
        if ([401, 403].includes(error.status)) signedOut();
      } finally {
        button.disabled = false;
        // Safari needs the blob to survive the click's task before its download starts.
        if (url) setTimeout(() => URL.revokeObjectURL(url), 30000);
      }
    },
  });
  return h("section", { class: "artifact" }, button, status);
}

function pictureNode(id) {
  const image = h("img", { src: scopedURL(`/api/media/${id}`), alt: "A picture", loading: "lazy", decoding: "async" });
  return h("button", { type: "button", class: "picture", "aria-label": "Open picture", dataset: { media: id } }, image);
}

function metaLine(turn) {
  const bits = [clockTime(turn.at)];
  if (turn.channel !== "web") bits.push(turn.direction === "in" ? `via ${turn.channel}` : `sent to ${turn.channel}`);
  if (turn.meta?.voice) bits.push("voice");
  if (turn.meta?.forwarded) bits.push("forwarded");
  return h("div", { class: "meta" }, bits.map((bit, i) => h("span", { text: bit, class: i === 0 ? "time" : "tag" })));
}

function pendingNode(p) {
  const words = p.voice ? "Voice message · transcribing…" : p.text;
  const node = h("article", { class: `turn mine pending ${p.state}`, dataset: { client: p.clientId } });
  const body = h("div", { class: "body" });
  if (p.files.length) {
    body.append(
      h(
        "div",
        { class: `pictures n${Math.min(p.files.length, 4)}` },
        p.files.map((file) => h("span", { class: "picture" }, h("img", { src: previewUrl(file), alt: "A picture you are sending" }))),
      ),
    );
  }
  if (words) body.append(h("p", { class: p.voice ? "words voice-note" : "words", text: words }));
  node.append(body);
  if (p.state === "sending") {
    node.append(h("div", { class: "meta" }, h("span", { class: "tag", text: "sending…" })));
  } else {
    const actions = h("div", { class: "pending-actions" });
    if (p.state === "failed") actions.append(h("button", { type: "button", class: "link", text: "Try again", onclick: () => deliver(p) }));
    else actions.append(h("button", { type: "button", class: "link", text: "Edit", onclick: () => editPending(p) }));
    actions.append(h("button", { type: "button", class: "link", text: "Delete", onclick: () => discardPending(p) }));
    node.append(h("div", { class: "meta error" }, h("span", { class: "tag", text: p.error ?? "Not sent." }), actions));
  }
  return node;
}

const previews = new WeakMap();
function previewUrl(file) {
  if (!previews.has(file)) previews.set(file, URL.createObjectURL(file));
  return previews.get(file);
}

function releasePreviews(files) {
  for (const file of files) {
    if (previews.has(file)) URL.revokeObjectURL(previews.get(file));
    previews.delete(file);
  }
}

// ---- approvals ------------------------------------------------------------------------------
function approvalNode(snapshot) {
  const approval = store.approval(snapshot);
  const card = h("section", { class: `approval ${approval.status}`, dataset: { approval: String(approval.id) } });
  card.append(
    h("div", { class: "approval-head" }, h("span", { class: "tool", text: approval.tool }), h("span", { class: "ticket", text: `#${approval.id}` })),
    h("pre", { class: "approval-summary", text: approval.summary }),
  );
  if (approval.status !== "pending") {
    const label = { approved: "Allowed", denied: "Declined", expired: "Expired" }[approval.status] ?? approval.status;
    card.append(h("div", { class: "stamp", text: label }));
    return card;
  }
  const note = h("input", { type: "text", class: "note", placeholder: "Tell it why (optional)", "aria-label": "Why you are declining", maxlength: "300", hidden: true });
  const allow = h("button", { type: "button", class: "primary allow", text: "Allow", onclick: () => decide(approval, "approve", card) });
  const decline = h("button", { type: "button", class: "decline", text: "Decline", onclick: () => {
    if (note.hidden) {
      note.hidden = false;
      note.focus();
      decline.textContent = "Send decline";
    } else {
      decide(approval, "deny", card, note.value.trim());
    }
  } });
  card.append(
    h("div", { class: "approval-actions" }, allow, decline),
    note,
    h("div", { class: "expires", dataset: { expires: String(approval.expires_at) }, text: expiryText(approval.expires_at) }),
  );
  return card;
}

function expiryText(expiresAt) {
  const left = expiresIn(expiresAt, nowSeconds());
  return left === "expired" ? "Expired: it will not run unless it is asked again." : `Expires in ${left} if nobody answers.`;
}

async function decide(approval, decision, card, note = "") {
  for (const button of card.querySelectorAll("button")) button.disabled = true;
  try {
    const result = await api(`/api/approvals/${approval.id}`, { method: "POST", json: { decision, ...(note ? { note } : {}) } });
    if (result.approval) store.setApprovals([result.approval]);
    if (!result.ok) toast(result.message, { error: false });
  } catch (error) {
    if (error?.name === "AbortError") return;
    if ([401, 403].includes(error.status)) return signedOut();
    toast(error.message, { error: true });
    for (const button of card.querySelectorAll("button")) button.disabled = false;
    return;
  }
  renderLog();
  renderStatus();
}

function showFirstWaiting() {
  const first = store.waiting[0];
  if (!first) return;
  const card = ui.log.querySelector(`[data-approval="${first.id}"]`);
  if (!card) return;
  card.scrollIntoView({ block: "center", behavior: reducedMotion() ? "auto" : "smooth" });
  card.classList.add("flash");
  setTimeout(() => card.classList.remove("flash"), 1600);
}

setInterval(() => {
  if (!ui) return;
  for (const node of ui.log.querySelectorAll(".expires[data-expires]")) {
    node.textContent = expiryText(Number(node.dataset.expires));
  }
}, 30_000);

// ---- clicks inside the conversation -------------------------------------------------------------
function onLogClick(event) {
  const copy = event.target.closest("[data-copy]");
  if (copy) {
    const code = copy.closest(".code")?.querySelector("pre code");
    if (code) copyText(code.textContent, copy);
    return;
  }
  const picture = event.target.closest("[data-media]");
  if (picture) openPicture(picture.dataset.media);
}

async function copyText(text, button) {
  try {
    await navigator.clipboard.writeText(text);
    button.textContent = "Copied";
  } catch {
    button.textContent = "Copy failed";
  }
  setTimeout(() => {
    button.textContent = "Copy";
  }, 1400);
}

function openPicture(id) {
  const dialog = h("dialog", { class: "lightbox", "aria-label": "Picture" }, h("img", { src: scopedURL(`/api/media/${id}`), alt: "A picture" }), iconButton("close", "Close", () => dialog.close(), "close"));
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
  dialog.addEventListener("close", () => dialog.remove());
  document.body.append(dialog);
  dialog.showModal();
}

// ---- the composer ---------------------------------------------------------------------------
function buildComposer() {
  const features = session.features;
  const files = [];
  const input = h("textarea", {
    class: "input",
    rows: "1",
    enterkeyhint: "send",
    placeholder: "Message",
    "aria-label": "Message",
    autocomplete: "off",
    autocapitalize: "sentences",
    maxlength: "20000",
  });
  const chips = h("div", { class: "chips", hidden: true });
  const hints = h("div", { class: "hints", hidden: true, role: "listbox", "aria-label": "Commands" });
  const picker = h("input", { type: "file", accept: "image/*", multiple: true, hidden: true });
  const attach = features.images ? iconButton("clip", "Attach a picture", () => picker.click(), "attach") : null;
  const send = iconButton("send", "Send", () => submit(), "send primary");
  const mic = features.voice && canRecord() ? iconButton("mic", "Record a voice message", () => startVoice(), "mic") : null;
  const voicePicker = h("input", { type: "file", accept: "audio/*", hidden: true });
  const audio = features.voice && !canRecord() ? iconButton("mic", "Send a voice file", () => voicePicker.click(), "mic") : null;
  const row = h("div", { class: "row" }, attach, input, mic, audio, send);
  const recording = h("div", { class: "recording", hidden: true });
  const element = h("footer", { class: "composer" }, hints, chips, row, recording, picker, voicePicker);
  const composer = { element, input, files, chips, hints, send, mic, row, recording, picker, voice: null };

  const refresh = () => {
    input.style.height = "auto";
    // A box that is not on the page yet measures as 0: leave it to CSS until it is. The border is
    // outside scrollHeight, and without it the box would scroll by those few pixels.
    if (input.isConnected) {
      const border = input.offsetHeight - input.clientHeight;
      input.style.height = `${Math.min(input.scrollHeight + border, 168)}px`;
    }
    const ready = input.value.trim() !== "" || files.length > 0;
    send.hidden = !ready && Boolean(mic);
    if (mic) mic.hidden = ready;
    const suggestions = commandHints(input.value);
    hints.hidden = suggestions.length === 0;
    hints.replaceChildren(
      ...suggestions.map(([name, about]) =>
        h("button", { type: "button", class: "hint", role: "option", onclick: () => { input.value = `${name} `; refresh(); input.focus(); } }, h("b", { text: name }), h("span", { text: about })),
      ),
    );
  };
  composer.refresh = refresh;
  input.addEventListener("input", refresh);
  input.addEventListener("keydown", (event) => {
    const coarse = matchMedia("(pointer: coarse)").matches;
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing && !coarse) {
      event.preventDefault();
      submit();
    }
  });
  input.addEventListener("focus", () => setTimeout(() => nearBottom() && scrollToBottom(false), 300));
  input.addEventListener("paste", (event) => {
    const pasted = [...(event.clipboardData?.files ?? [])].filter((f) => f.type.startsWith("image/"));
    if (pasted.length && features.images) {
      event.preventDefault();
      addFiles(pasted);
    }
  });
  picker.addEventListener("change", () => {
    addFiles([...picker.files]);
    picker.value = "";
  });
  voicePicker.addEventListener("change", async () => {
    const file = voicePicker.files[0];
    voicePicker.value = "";
    if (!file) return;
    if (file.size > features.max_voice_bytes) return toast("That voice file is too large.", { error: true });
    const seconds = await audioDuration(file);
    const entry = store.addPending({ clientId: newClientId(), text: "", voice: { blob: file, name: file.name, seconds }, at: nowSeconds() });
    renderLog();
    scrollToBottom(false);
    await deliver(entry);
  });
  root.addEventListener("dragover", (event) => {
    if (event.dataTransfer?.types?.includes("Files")) event.preventDefault();
  });
  root.addEventListener("drop", (event) => {
    const dropped = [...(event.dataTransfer?.files ?? [])].filter((f) => f.type.startsWith("image/"));
    if (dropped.length && features.images) {
      event.preventDefault();
      addFiles(dropped);
    }
  });
  refresh();
  return composer;
}

async function addFiles(list) {
  const composer = ui.composer;
  const room = session.features.max_images - composer.files.length;
  if (list.length > room) toast(`At most ${session.features.max_images} pictures per message.`, { error: true });
  for (const original of list.slice(0, Math.max(room, 0))) {
    const file = await shrinkImage(original, { maxBytes: session.features.max_image_bytes });
    composer.files.push(file);
  }
  renderChips();
  composer.refresh();
}

function renderChips() {
  const { chips, files } = ui.composer;
  chips.hidden = files.length === 0;
  chips.replaceChildren(
    ...files.map((file) =>
      h(
        "span",
        { class: "chip" },
        h("img", { src: previewUrl(file), alt: "Attached picture" }),
        h("button", { type: "button", class: "chip-remove", "aria-label": "Remove picture", onclick: () => {
          files.splice(files.indexOf(file), 1);
          releasePreviews([file]);
          renderChips();
          ui.composer.refresh();
        } }, icon("close")),
      ),
    ),
  );
}

function clearComposer() {
  const composer = ui.composer;
  composer.input.value = "";
  composer.files.length = 0;
  renderChips();
  composer.refresh();
}

async function submit() {
  const composer = ui.composer;
  const text = composer.input.value.trim();
  const files = composer.files.slice();
  if (!text && files.length === 0) return;
  composer.files.length = 0; // the entry owns them now
  clearComposer();
  const entry = store.addPending({ clientId: newClientId(), text, files, at: nowSeconds() });
  renderLog();
  scrollToBottom(false);
  composer.input.focus({ preventScroll: true });
  await deliver(entry);
}

async function deliver(entry) {
  entry.state = "sending";
  entry.error = null;
  renderLog();
  const form = new FormData();
  form.set("text", entry.text);
  form.set("client_id", entry.clientId);
  for (const file of entry.files) form.append("image", file, file.name || "photo.jpg");
  if (entry.voice) {
    form.set("voice", entry.voice.blob, entry.voice.name);
    if (Number.isFinite(entry.voice.seconds)) form.set("voice_seconds", String(entry.voice.seconds));
  }
  try {
    const result = await api("/api/messages", { method: "POST", form });
    releasePreviews(entry.files);
    const fresh = store.settle(entry.clientId, result.turn);
    renderLog();
    if (fresh.length) scrollToBottom(true);
  } catch (error) {
    if (!store.findPending(entry.clientId)) return; // the stream already showed it as delivered
    if (error?.name === "AbortError") return;
    if ([401, 403].includes(error.status)) return signedOut();
    entry.state = error.status === 422 ? "rejected" : "failed";
    entry.error = error.status === 0 ? "Not sent: no connection." : error.message;
    renderLog();
  }
}

function editPending(p) {
  const composer = ui.composer;
  composer.input.value = p.text;
  composer.files.push(...p.files);
  store.dropPending(p.clientId);
  renderChips();
  composer.refresh();
  renderLog();
  composer.input.focus();
}

function discardPending(p) {
  releasePreviews(p.files);
  store.dropPending(p.clientId);
  renderLog();
}

// ---- voice ----------------------------------------------------------------------------------
async function startVoice() {
  const composer = ui.composer;
  if (composer.voice) return;
  const limit = session.features.voice_seconds;
  const clock = h("span", { class: "rec-clock", text: "0:00" });
  let cancelled = false;
  let take;
  try {
    take = await record({ maxSeconds: limit, onTick: (seconds) => { clock.textContent = `${durationLabel(seconds)} / ${durationLabel(limit)}`; } });
  } catch (error) {
    const denied = error?.name === "NotAllowedError";
    toast(denied ? "The microphone is blocked for this page. Allow it in the browser's settings." : "Could not start the microphone.", { error: true });
    return;
  }
  composer.voice = take;
  composer.row.hidden = true;
  composer.recording.hidden = false;
  composer.recording.replaceChildren(
    iconButton("close", "Discard the recording", () => { cancelled = true; take.cancel(); }, "discard"),
    h("span", { class: "rec-dot", "aria-hidden": "true" }),
    clock,
    iconButton("send", "Send the recording", () => take.stop(), "send primary"),
  );
  try {
    const result = await take.done;
    if (cancelled || !result) return;
    const entry = store.addPending({ clientId: newClientId(), text: "", voice: result, at: nowSeconds() });
    renderLog();
    scrollToBottom(false);
    await deliver(entry);
  } catch {
    toast("The recording failed.", { error: true });
  } finally {
    composer.voice = null;
    composer.recording.hidden = true;
    composer.row.hidden = false;
  }
}

// ---- settings -------------------------------------------------------------------------------
async function openSettings() {
  const status = h("pre", { class: "status-text", text: "Asking…" });
  const activity = h("ol", { class: "activity-list", "aria-live": "polite" });
  const connection = h("p", { class: "about", text: connectionLabel(session.connection) });
  const refreshStatus = async () => {
    try {
      const data = await api("/api/status");
      status.textContent = data.text;
      connection.textContent = `${connectionLabel(data.connection)} · ${data.connection.address}`;
      const items = data.activity?.items ?? [];
      activity.replaceChildren(...(items.length ? items.map((item) => h("li", {},
        h("strong", { text: item.title }),
        h("p", { class: "about", text: activityLabel(item) }),
      )) : [h("li", { class: "about", text: "No recent tasks." })]));
    } catch (error) {
      status.textContent = error.message;
    }
  };
  const choice = h("div", { class: "segments", role: "radiogroup", "aria-label": "Appearance" });
  const paint = () => {
    choice.replaceChildren(
      ...THEMES.map((value) =>
        h("button", { type: "button", role: "radio", "aria-checked": String(theme() === value), class: theme() === value ? "on" : "", text: value[0].toUpperCase() + value.slice(1), onclick: () => { localStorage.setItem("pa-theme", value); applyTheme(value); paint(); } }),
      ),
    );
  };
  paint();
  const hints = [];
  if (!canRecord() && session.features.voice) hints.push("You can send a recorded audio file here. Recording with the microphone needs HTTPS; connect through Tailscale to enable it.");
  const ios = /iPhone|iPad/.test(navigator.userAgent);
  const installed = matchMedia("(display-mode: standalone)").matches || navigator.standalone;
  if (ios && !installed) hints.push("To use this like an app: Share, then Add to Home Screen. Pair again from the new icon, which keeps its own sign-in.");
  const dialog = h(
    "dialog",
    { class: "sheet", "aria-label": "Settings" },
    h("header", {}, h("h2", { text: session.agent }), iconButton("close", "Close", () => dialog.close(), "close")),
    h("section", {}, h("h3", { text: "Right now" }), status),
    h("section", {}, h("h3", { text: "Recent tasks" }), activity, h("button", { type: "button", text: "Refresh status", onclick: refreshStatus })),
    h("section", {}, h("h3", { text: "Connection" }), connection),
    h("section", {}, h("h3", { text: "Appearance" }), choice),
    h(
      "section",
      {},
      h("h3", { text: "This browser" }),
      h("p", { class: "about", text: `Paired as “${session.label}”. Version ${session.version}.` }),
      h("button", { type: "button", class: "danger", text: "Sign out of this browser", onclick: async () => {
        try {
          await api("/api/logout", { method: "POST" });
        } catch (error) {
          if (error.status !== 401) return toast(error.message, { error: true });
        }
        dialog.close();
        signedOut();
      } }),
    ),
    hints.length ? h("section", {}, h("h3", { text: "Good to know" }), hints.map((hint) => h("p", { class: "about", text: hint }))) : null,
  );
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
  dialog.addEventListener("close", () => dialog.remove());
  document.body.append(dialog);
  dialog.showModal();
  await refreshStatus();
}

// ---- the phone's keyboard, the badge, coming back from the background ---------------------------
function watchViewport() {
  const viewport = window.visualViewport;
  if (!viewport) return;
  const fit = () => {
    const keyboard = window.innerHeight - viewport.height > 120;
    root.style.height = keyboard ? `${viewport.height}px` : "";
    root.style.transform = keyboard ? `translateY(${viewport.offsetTop}px)` : "";
    if (keyboard && ui && pinned) scrollToBottom(false);
  };
  viewport.addEventListener("resize", fit);
  viewport.addEventListener("scroll", fit);
}

function setBadge(count) {
  document.title = (count ? `(${count}) ` : "") + (session?.agent ?? document.title.replace(/^\(\d+\) /, ""));
  try {
    if (count) navigator.setAppBadge?.(count);
    else navigator.clearAppBadge?.();
  } catch {
    // a badge is a courtesy
  }
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden || !live) return;
  unread = 0;
  setBadge(0);
  live.reconnect(); // a phone that slept may have left the stream half open
});
window.addEventListener("online", () => live?.reconnect());
window.addEventListener("pageshow", (event) => {
  if (event.persisted) live?.reconnect();
});

main();
