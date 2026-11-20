// The page. It draws what the server's database says and sends what the owner types; it keeps
// nothing of its own that the server does not also have, so closing it, losing the connection or
// opening it on another phone costs nothing.
//
// Everything is built with DOM calls and `textContent`. There is no innerHTML anywhere in this
// file, and none may be added: the page's policy forbids inline code, and the answers it shows can
// repeat text that someone else wrote.

import { Live, api, artifactBlob, scopedArtifactBlob, agentSetupBlob, scopedURL, invalidateRequests, setRequestContext, onAccessLost } from "./api.js";
import { createAssistantCards, createAssistantWelcome } from "./assistant.js";
import { createAgentMark, createProductShell } from "./product-shell.js";
import { activityLabel, connectionLabel, toolLabel, toolProgress } from "./activity.js";
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
import { presentSheet, dismissSheet } from "./sheet.js";
import { Store } from "./store.js";

const PAGE_VERSION = new URL(import.meta.url).searchParams.get("v") ?? "";
const STICK_PX = 140;
const KEPT_NOTHING =
  "配对已完成，但浏览器没有保留登录状态。请允许此网站使用 Cookie，并确认使用的是同一个访问地址。";
const THEMES = ["auto", "light", "dark"];

const root = document.getElementById("app");
const store = new Store();
let session = null;
let live = null;
let ui = null; // the chat's elements, once it is built
let unread = 0;
let pinned = true; // the log was at its end the last time it moved
let settled = false; // the first screenful of history is on the page: from here on, new things arrive with a flourish
const entered = new Set(); // keys of nodes that have already made their entrance
const rendered = new Map(); // key -> { node, signature }
const nowSeconds = () => Date.now() / 1000;
onAccessLost(() => { if (ui) signedOut(); });

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
  lock: "M7.5 11V8a4.5 4.5 0 0 1 9 0v3M6 11h12v9H6z",
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
  const view = ui;
  const node = h("div", { class: `toast${error ? " error" : ""}`, role: error ? "alert" : "status", text: message });
  const leave = () => {
    if (node.dataset.leaving) return;
    node.dataset.leaving = "true";
    node.className = `${node.className} leaving`;
    setTimeout(() => node.remove(), 280);
  };
  node.addEventListener("click", leave);
  // Three at once is already too many to read: the oldest makes way.
  while (view.toasts.children.length >= 3) view.toasts.children[0].remove();
  view.toasts.append(node);
  setTimeout(leave, error ? 7000 : 3500);
}

/** True the first time a node is asked, once the opening screenful is on the page. */
function entrance(key) {
  if (!settled || entered.has(key)) return false;
  entered.add(key);
  return true;
}

function reducedMotion() {
  return matchMedia("(prefers-reduced-motion: reduce)").matches;
}

// ---- theme ----------------------------------------------------------------------------------
function theme() {
  try {
    const stored = localStorage.getItem("pa-theme");
    return THEMES.includes(stored) ? stored : "auto";
  } catch { return "auto"; }
}

function applyTheme(value) {
  document.documentElement.dataset.theme = value;
  const dark = value === "dark" || (value === "auto" && matchMedia("(prefers-color-scheme: dark)").matches);
  for (const meta of document.querySelectorAll('meta[name="theme-color"]')) {
    if (!meta.dataset.original) meta.dataset.original = meta.content;
    meta.content = value === "auto" ? meta.dataset.original : dark ? "#111614" : "#f5f4ef";
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
  if (location.hash && location.hash !== "#assistant-inbox") history.replaceState(null, "", location.pathname + location.search);
  return code;
}

// ---- pairing --------------------------------------------------------------------------------
function showPairing(problem = "") {
  teardownChat();
  const input = h("input", {
    type: "password",
    name: "code",
    autocomplete: "off",
    autocapitalize: "off",
    spellcheck: "false",
    placeholder: "粘贴配对链接或代码",
    "aria-label": "配对链接或代码",
  });
  const error = h("p", { class: "form-error", role: "alert", text: problem });
  const button = h("button", { type: "submit", class: "primary", text: "连接我的助理" });
  const form = h("form", { class: "pair-form" }, input, button);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const code = parsePairing(input.value);
    if (!code) {
      error.textContent = "配对链接或代码不完整，请复制完整内容。";
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
      createAgentMark(document, "pair-brand-mark", "idle"),
      h("p", { class: "assistant-eyebrow", text: "Personal Agent" }),
      h("h1", { text: "把日常，交给你的助理。" }),
      h("p", { class: "lede", text: "聊天、整理资料、安排提醒，也把需要执行的事交办好。连接账号，从这里继续。" }),
      h(
        "ol",
        { class: "steps" },
        h("li", {}, "在已登录设备的「设备与账号」中，生成本人配对链接。"),
        h("li", {}, "在这里粘贴链接或代码，继续使用同一个账号。"),
      ),
      form,
      error,
      h(
        "p",
        { class: "fine-print" },
        "配对链接限本人使用，只能使用一次。",
      ),
      h("div", { class: "pair-options" }, h("a", { href: "/workspace", text: "恢复账号或接受项目邀请" })),
      h("details", {}, h("summary", { text: "首次连接自己的助理" }), h("p", {}, "在运行助理的电脑上执行 ", h("code", { text: "pa web pair" }), "，将生成的链接粘贴到上方。")),
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
      createAgentMark(document, "pair-brand-mark", "offline"),
      h("h1", { text: document.title || "Agent" }),
      h("p", { class: "lede", text: message }),
      h("button", { type: "button", class: "primary", text: "重新连接", onclick: () => start() }),
    ),
  );
}

function signedOut() {
  teardownChat();
  showPairing("当前设备已退出，请重新配对后继续。");
}

// ---- the chat -------------------------------------------------------------------------------
function teardownChat() {
  invalidateRequests();
  ui?.shell?.destroy();
  ui?.cards?.destroy();
  ui?.composer?.destroy();
  ui?.resizeObserver?.disconnect();
  ui?.frameObserver?.disconnect();
  ui?.viewportCleanup?.();
  // These overlays live outside #app. A revoked session must not leave old task or image
  // content visible above the sign-in screen, even if a request is still settling.
  for (const dialog of document.querySelectorAll("dialog")) {
    dialog.close();
    dialog.replaceChildren();
    dialog.remove();
  }
  for (const entry of store.pending) releasePreviews(entry.files);
  store.principalId = null;
  if (live) live.stop();
  live = null;
  ui = null;
  rendered.clear();
  entered.clear();
  settled = false;
  store.turns.clear();
  store.order = [];
  store.pending = [];
  store.approvals.clear();
  store.drafts.clear();
  store.completedRuns.clear();
  store.more = false;
  store.setWorking(false);
  setBadge(0);
  unread = 0;
}

async function showChat(notice) {
  store.principalId = session.principal_id;
  setRequestContext(session.principal_id);
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
  if (store.timeline.length) scrollToBottom(false);
  else ui.log.scrollTo({ top: 0, behavior: "auto" });
  settled = true;
  const view = ui;
  void ui.cards.refresh().then(() => { if (ui === view && location.hash === "#assistant-inbox") ui.cards.focus(); });
  void loadAssistantSpaces();
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
      renderStatus();
      if (stick) scrollToBottom(false);
    },
    onAssistantChanged: () => ui?.cards?.refresh(),
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
  const menu = iconButton("more", "设置", openSettings);
  const banner = h("div", { class: "banner", role: "status", hidden: true });
  const update = h("button", { type: "button", class: "update", hidden: true, text: "有新版本可用，点击更新。", onclick: () => location.reload() });
  const loadMore = h("button", { type: "button", class: "earlier", hidden: true, text: "查看更早的对话", onclick: loadEarlier });
  const working = h("div", { class: "working", hidden: true, "aria-hidden": "true" }, createAgentMark(document, "", "busy"), h("span", { class: "working-text" }));
  const log = h("main", { class: "log", id: "log", role: "log", "aria-live": "polite", "aria-label": "个人助理对话", tabindex: "-1" }, loadMore, working);
  const jump = h("button", { type: "button", class: "jump", hidden: true, onclick: () => scrollToBottom(true) }, icon("down"), h("span", { class: "jump-count" }));
  const toasts = h("div", { class: "toasts" });
  const mark = createAgentMark(document, "chat-brand-mark", "idle");
  ui = { name, lamp, state, mark, waiting, banner, update, log, loadMore, working, jump, toasts, composer: buildComposer() };
  ui.welcome = createAssistantWelcome({ document, onPrompt: setAssistantPrompt });
  ui.cardCount = 0;
  ui.cards = createAssistantCards({
    document, api, onPrompt: setAssistantPrompt,
    onContextChanged: change => { if (change?.leftSpace) location.assign("/"); else void loadAssistantSpaces(); },
    onChange: (count, detail) => { if (ui) { ui.cardCount = count; ui.cardTotal = detail?.total || 0; renderChatLayout(); renderStatus(); } },
    onNotice: (message, error = false) => toast(message, { error }),
    downloadConnection: async (agentId, kind) => {
      const view = ui, blob = await agentSetupBlob(agentId, kind);
      if (!ui || ui !== view) return;
      const name = kind === "openapi" ? "personal-agent-openapi.json" : "pa-agent-bridge.py";
      const url = URL.createObjectURL(blob), link = h("a", { href: url, download: name, hidden: true });
      document.body.append(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 60_000);
    },
    downloadArtifact: async (path, name) => {
      const view = ui, match = /^\/api\/artifacts\/([0-9a-f]{32})$/.exec(path);
      const blob = await (match ? artifactBlob(match[1]) : scopedArtifactBlob(path));
      if (!ui || ui !== view) return;
      const url = URL.createObjectURL(blob), link = h("a", { href: url, download: name, hidden: true });
      document.body.append(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 60_000);
    },
  });
  ui.spaceSelect = h("select", { "aria-label": "当前空间", class: "assistant-space-select", hidden: true });
  ui.spaceSelect.addEventListener("change", () => {
    const url = new URL("/", location.origin); url.searchParams.set("space", ui.spaceSelect.value); location.assign(url.pathname + url.search);
  });
  pinned = true;
  ui.log.addEventListener("scroll", onScroll, { passive: true });
  ui.log.addEventListener("click", onLogClick);
  // A picture makes its message taller once it has loaded: if the log was at its end, stay there.
  ui.log.addEventListener("load", () => pinned && scrollToBottom(false), true);
  // So does anything that makes the log itself shorter (a banner, the phone's keyboard).
  if (typeof ResizeObserver === "function") {
    const view = ui;
    ui.resizeObserver = new ResizeObserver(() => ui === view && pinned && scrollToBottom(false));
    ui.resizeObserver.observe(ui.log);
  }
  const header = h("header", { class: "bar" }, h("div", { class: "who" }, mark, h("div", { class: "who-copy" }, name, h("span", { class: "chat-presence" }, lamp, state))), h("div", { class: "bar-actions" }, waiting, menu));
  ui.page = h("div", { class: "chat" }, header, banner, update, h("div", { class: "stage" }, log, jump), toasts, ui.composer.element);
  // The header and the composer float over the log, which leaves room for exactly their height:
  // as a note appears, a second line is typed or the phone's safe area changes, the room follows.
  if (typeof ResizeObserver === "function") {
    const view = ui, page = ui.page, composer = ui.composer.element;
    const measure = () => {
      if (ui !== view) return;
      page.style.setProperty("--bar-h", `${header.offsetHeight}px`);
      page.style.setProperty("--composer-h", `${composer.offsetHeight}px`);
      if (pinned) scrollToBottom(false);
    };
    ui.frameObserver = new ResizeObserver(measure);
    ui.frameObserver.observe(header);
    ui.frameObserver.observe(composer);
  }
  ui.shell = createProductShell({ document, content: ui.page, current: "chat", resolve: scopedURL, spaceControl: ui.spaceSelect, onSettings: openSettings });
  header.prepend(ui.shell.toggle);
  root.replaceChildren(ui.shell.element);
  ui.composer.refresh(); // now that it is on the page it can be measured
  renderStatus();
  ui.viewportCleanup = watchViewport();
}

function setAssistantPrompt(text) {
  if (!ui) return;
  const input = ui.composer.input;
  // Shortcuts prepare a normal message. They never send it or overwrite a drafted message.
  input.value = input.value.trim() ? `${input.value}\n${text}` : text;
  ui.composer.refresh(); input.focus();
}

async function loadAssistantSpaces() {
  const view = ui;
  try {
    const context = await api("/api/v3/me");
    if (!ui || ui !== view) return;
    const choices = (context.spaces || []).map(space => h("option", { value: space.id, text: space.name || (space.kind === "personal" ? "个人空间" : "项目空间") }));
    ui.spaceSelect.replaceChildren(...choices); ui.spaceSelect.value = context.space_id;
    ui.spaceSelect.hidden = choices.length === 0;
    const space = (context.spaces || []).find(item => item.id === context.space_id);
    ui.shell.setContext({ name: context.principal?.display_name, space: space?.name, role: space?.kind === "personal" ? "仅自己可见" : { owner: "负责人", member: "成员", viewer: "访客" }[context.role] });
  } catch (error) { if (error?.name !== "AbortError" && ui === view) ui.spaceSelect.hidden = true; }
}

// ---- the stream's events ----------------------------------------------------------------------
function onHello(data) {
  store.setWorking(data.working);
  store.setApprovals(data.approvals ?? []);
  store.replaceDrafts(data.drafts);
  renderLog();
  renderStatus();
  void ui?.cards?.refresh();
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
  if (turn.direction === "out") void ui?.cards?.refresh();
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
      ui.banner.textContent = navigator.onLine === false ? "当前离线，等待网络恢复…" : "正在重新连接你的助理…";
      ui.banner.hidden = false;
    }, 2500);
  }
}

function renderStatus() {
  if (!ui) return;
  const offline = Boolean(live) && live.state === "down";
  const waiting = store.waiting.length + (ui.cardCount || 0);
  // Once words are arriving they are the sign of life; the capsule would only say it twice.
  const streaming = store.working && [...store.drafts.values()].some((draft) => draft.text);
  ui.lamp.className = `lamp ${offline ? "down" : store.working ? "on" : "idle"}`;
  ui.state.textContent = offline ? "连接中…" : streaming ? "正在回复…" : store.working ? toolProgress(store.tool) : store.waiting.length ? "等你确认" : "随时可以开始";
  ui.mark.setAttribute("data-state", offline ? "offline" : store.working ? "busy" : waiting ? "attention" : "idle");
  const showCapsule = store.working && !streaming;
  const appearing = ui.working.hidden && showCapsule;
  const stick = appearing && nearBottom(); // the indicator is the newest line: keep it in view
  ui.working.hidden = !showCapsule;
  ui.working.querySelector(".working-text").textContent = toolProgress(store.tool);
  if (stick) scrollToBottom(!document.hidden);
  ui.waiting.hidden = waiting === 0;
  ui.waiting.textContent = `${waiting} 项待处理`;
  renderChatLayout();
}

function renderChatLayout() {
  if (!ui?.page) return;
  ui.page.classList.toggle("chat-empty", !store.timeline.length && !store.drafts.size && !store.working && !ui.cardTotal);
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
  ui.jump.querySelector(".jump-count").textContent = count > 1 ? `${count} 条新消息` : "新消息";
  ui.jump.hidden = false;
}

function onScroll() {
  pinned = nearBottom();
  const moved = ui.log.scrollTop > 6;
  if ((ui.page.dataset.scrolled === "true") !== moved) ui.page.dataset.scrolled = String(moved);
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
    const wasSettled = settled;
    settled = false;
    renderLog();
    settled = wasSettled;
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
    wanted.push(draftView(key, draft));
    seen.add(key);
  }
  if (!wanted.length && ui.welcome) wanted.push(ui.welcome);
  if (ui.cards) wanted.push(ui.cards.element);
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
  renderChatLayout();
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

// A reply being written keeps one node for its whole life and is typeset again as words arrive:
// it is never plain text that suddenly turns into a document, never replays its entrance, and
// never loses a selection or a scroll position because a new word came in.
function draftView(key, draft) {
  let known = rendered.get(key);
  if (!known) {
    known = { node: draftNode(entrance(key)), signature: "" };
    rendered.set(key, known);
  }
  const signature = `${draft.sequence}|${draft.text.length}|${draft.truncated ? 1 : 0}`;
  if (known.signature !== signature) {
    known.signature = signature;
    known.node.querySelector(".body").replaceChildren(renderMarkdown(document, draft.text));
    known.node.querySelector(".tag").textContent = draft.truncated ? "正在整理完整回复…" : "正在回复…";
  }
  return known.node;
}

function draftNode(fresh) {
  return h("article", { class: `turn agent streaming${fresh ? " fresh" : ""}`, "aria-busy": "true" },
    h("div", { class: "speaker" }, createAgentMark(document, "", "busy"), session.agent),
    h("div", { class: "body" }),
    h("div", { class: "meta" }, h("span", { class: "tag" })),
  );
}

const approvalState = (turn) => (turn.approvals ?? []).map((a) => `${a.id}:${store.approval(a).status}`).join(",");
const turnSignature = (turn, previous) => `${turn.id}|${previous?.direction ?? "-"}|${approvalState(turn)}`;
const pendingSignature = (p) => `${p.state}|${p.error ?? ""}`;

function turnNode(turn, previous) {
  const mine = turn.direction === "in";
  // What I send already made its entrance as a pending bubble; only the answer arrives anew.
  const fresh = !mine && entrance(`t${turn.id}`);
  const node = h("article", { class: `turn ${mine ? "mine" : "agent"}${fresh ? " fresh" : ""}`, dataset: { id: String(turn.id), kind: turn.kind } });
  if (!mine && (!previous || previous.direction === "in")) node.append(h("div", { class: "speaker" }, createAgentMark(document), session.agent));
  const body = h("div", { class: "body" });
  const pictures = (turn.meta?.attachments ?? []).filter((a) => a.kind === "image" && /^[0-9a-f]{64}$/.test(a.id));
  if (pictures.length) body.append(h("div", { class: `pictures n${Math.min(pictures.length, 4)}` }, pictures.map((a) => pictureNode(a.id))));
  if (mine) {
    if (turn.text) body.append(h("p", { class: turn.text.startsWith("/") ? "words command" : "words", text: turn.text }));
  } else if (!(turn.kind === "approval_request" && (turn.approvals ?? []).length)) {
    // An approval's text is the fallback for chat apps that cannot show buttons ("Reply /approve 1").
    // Here the card below carries the same request with the buttons themselves.
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
  const view = ui;
  const name = file.name.slice(0, 240);
  const expired = Number.isFinite(file.expires_at) && file.expires_at <= nowSeconds();
  const size = Number.isSafeInteger(file.size) && file.size >= 0
    ? (file.size < 1024 ? `${file.size} B` : `${Math.ceil(file.size / 1024)} KB`)
    : "文件";
  const status = h("span", { class: "artifact-status", role: "status", text: expired ? "文件已过期，可以请助理重新生成" : size });
  const button = h("button", {
    type: "button", class: "artifact-download", disabled: expired, text: `下载 ${name}`,
    onclick: async () => {
      if (!view || ui !== view) return;
      button.disabled = true;
      status.textContent = "正在下载…";
      let url;
      try {
        const blob = await artifactBlob(file.id);
        if (ui !== view) return;
        url = URL.createObjectURL(blob);
        const link = h("a", { href: url, download: name });
        document.body.append(link);
        link.click();
        link.remove();
        status.textContent = size;
      } catch (error) {
        if (ui !== view || error?.name === "AbortError") return;
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
  const image = h("img", { src: scopedURL(`/api/media/${id}`), alt: "对话中的图片", loading: "lazy", decoding: "async" });
  return h("button", { type: "button", class: "picture", "aria-label": "查看图片", dataset: { media: id } }, image);
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
  const node = h("article", { class: `turn mine pending ${p.state}${entrance(`p${p.clientId}`) ? " fresh" : ""}`, dataset: { client: p.clientId } });
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
    node.append(h("div", { class: "meta" }, h("span", { class: "tag", text: "正在发送…" })));
  } else {
    const actions = h("div", { class: "pending-actions" });
    if (p.state === "failed") actions.append(h("button", { type: "button", class: "link", text: "重新发送", onclick: () => deliver(p) }));
    else actions.append(h("button", { type: "button", class: "link", text: "修改消息", onclick: () => editPending(p) }));
    actions.append(h("button", { type: "button", class: "link", text: "移除", onclick: () => discardPending(p) }));
    node.append(h("div", { class: "meta error" }, h("span", { class: "tag", text: p.error ?? "尚未发送。" }), actions));
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
  const action = h("div", { class: "tool", title: approval.tool, text: toolLabel(approval.tool) });
  const summary = h("pre", { class: "approval-summary", text: approval.summary });
  if (approval.status !== "pending") {
    const label = { approved: "已允许", denied: "未允许", expired: "已过期", superseded: "已被新消息取代" }[approval.status] ?? approval.status;
    card.append(h("div", { class: "approval-head" }, action, h("span", { class: "stamp", text: label })), summary);
    return card;
  }
  card.append(
    h("div", { class: "approval-head" }, h("span", { class: "approval-kind" }, icon("lock"), "需要你确认"), h("span", { class: "ticket", text: `#${approval.id}` })),
    action,
    summary,
  );
  const note = h("input", { type: "text", class: "note", placeholder: "告诉助理原因（可选）", "aria-label": "不允许的原因", maxlength: "300", hidden: true });
  const allow = h("button", { type: "button", class: "primary allow", text: "允许执行", onclick: () => decide(approval, "approve", card) });
  const decline = h("button", { type: "button", class: "decline", text: "不允许", onclick: () => {
    if (note.hidden) {
      note.hidden = false;
      note.focus();
      decline.textContent = "确认不允许";
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
  return expiresAt <= nowSeconds() ? "确认已过期，这次操作不会执行。" : `${left}内未确认，本次操作将过期。`;
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
  if (!first) return ui?.cards?.focus();
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
    button.textContent = "已复制";
  } catch {
    button.textContent = "复制失败";
  }
  setTimeout(() => {
    button.textContent = "复制";
  }, 1400);
}

function openPicture(id) {
  const dialog = h("dialog", { class: "lightbox", "aria-label": "查看图片" }, h("img", { src: scopedURL(`/api/media/${id}`), alt: "对话中的图片" }), iconButton("close", "关闭", () => dismissSheet(dialog), "close"));
  dialog.addEventListener("close", () => dialog.remove());
  presentSheet(dialog, { document });
}

// ---- the composer ---------------------------------------------------------------------------
function buildComposer() {
  const features = session.features;
  const files = [];
  const input = h("textarea", {
    class: "input",
    rows: "1",
    enterkeyhint: "send",
    placeholder: "说说你想做的事…",
    "aria-label": "给个人助理发消息",
    autocomplete: "off",
    autocapitalize: "sentences",
    maxlength: "20000",
  });
  const chips = h("div", { class: "chips", hidden: true });
  const hints = h("div", { class: "hints", hidden: true, role: "group", "aria-label": "快捷指令" });
  const picker = h("input", { type: "file", accept: "image/*", multiple: true, hidden: true });
  const attach = features.images ? iconButton("clip", "添加图片", () => picker.click(), "attach") : null;
  const send = iconButton("send", "发送", () => submit(), "send primary");
  const mic = features.voice && canRecord() ? iconButton("mic", "录制语音", () => startVoice(), "mic") : null;
  const voicePicker = h("input", { type: "file", accept: "audio/*", hidden: true });
  const audio = features.voice && !canRecord() ? iconButton("mic", "发送语音文件", () => voicePicker.click(), "mic") : null;
  // Writing and sending sit on one line, like a message: the microphone becomes the send
  // button the moment there is something to send, and back again when the field is empty.
  const actions = h("div", { class: "row-actions" }, mic, audio, send);
  const row = h("div", { class: "row" }, attach, input, actions);
  const recording = h("div", { class: "recording", hidden: true });
  const element = h("footer", { class: "composer" }, hints, chips, row, recording, picker, voicePicker, h("p", { class: "composer-note" }, "先聊想法，再做决定。需要确认时，我会等你。", h("span", { class: "keys", text: "按 Enter 发送，Shift + Enter 换行。" })));
  const composer = { element, input, files, chips, hints, send, mic, row, recording, picker, voice: null, voiceStarting: false, pendingImages: 0, pendingAudio: 0, disposed: false };
  const current = () => !composer.disposed && ui?.composer === composer;

  const refresh = () => {
    input.style.height = "auto";
    // A box that is not on the page yet measures as 0: leave it to CSS until it is. The border is
    // outside scrollHeight, and without it the box would scroll by those few pixels.
    if (input.isConnected) {
      const border = input.offsetHeight - input.clientHeight;
      input.style.height = `${Math.min(input.scrollHeight + border, 168)}px`;
    }
    const ready = input.value.trim() !== "" || files.length > 0;
    send.disabled = !ready || composer.pendingImages > 0 || composer.voiceStarting;
    send.hidden = !ready && Boolean(mic);
    if (mic) { mic.hidden = ready; mic.disabled = composer.voiceStarting || composer.pendingImages > 0; }
    const suggestions = commandHints(input.value);
    hints.hidden = suggestions.length === 0;
    hints.replaceChildren(
      ...suggestions.map(([name, about]) =>
        h("button", { type: "button", class: "hint", onclick: () => { input.value = `${name} `; refresh(); input.focus(); } }, h("b", { text: name }), h("span", { text: about })),
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
  input.addEventListener("focus", () => setTimeout(() => current() && nearBottom() && scrollToBottom(false), 300));
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
    if (!file || !current()) return;
    if (file.size > features.max_voice_bytes) return toast("语音文件太大，请选择更短的录音。", { error: true });
    composer.pendingAudio += 1;
    let seconds;
    try { seconds = await audioDuration(file); }
    finally { composer.pendingAudio = Math.max(0, composer.pendingAudio - 1); }
    if (!current()) return;
    const entry = store.addPending({ clientId: newClientId(), text: "", voice: { blob: file, name: file.name, seconds }, at: nowSeconds() });
    renderLog();
    scrollToBottom(false);
    await deliver(entry);
  });
  const dragover = (event) => {
    if (current() && event.dataTransfer?.types?.includes("Files")) event.preventDefault();
  };
  const drop = (event) => {
    if (!current()) return;
    const dropped = [...(event.dataTransfer?.files ?? [])].filter((f) => f.type.startsWith("image/"));
    if (dropped.length && features.images) {
      event.preventDefault();
      addFiles(dropped);
    }
  };
  root.addEventListener("dragover", dragover);
  root.addEventListener("drop", drop);
  const beforeUnload = event => {
    if (!current()) return;
    if (input.value.trim() || files.length || composer.pendingImages || composer.pendingAudio || composer.voiceStarting || composer.voice) {
      event.preventDefault();
      event.returnValue = "";
    }
  };
  window.addEventListener("beforeunload", beforeUnload);
  composer.destroy = () => {
    composer.disposed = true;
    composer.voice?.cancel();
    composer.voice = null;
    composer.voiceStarting = false;
    composer.pendingImages = 0;
    composer.pendingAudio = 0;
    root.removeEventListener("dragover", dragover);
    root.removeEventListener("drop", drop);
    window.removeEventListener("beforeunload", beforeUnload);
    releasePreviews(files);
    files.length = 0;
    input.value = ""; picker.value = ""; voicePicker.value = "";
    chips.replaceChildren();
  };
  refresh();
  return composer;
}

async function addFiles(list) {
  const view = ui;
  if (!view) return;
  const composer = view.composer, features = session.features;
  if (composer.disposed) return;
  const room = features.max_images - composer.files.length - composer.pendingImages;
  if (list.length > room) toast(`每条消息最多添加 ${features.max_images} 张图片。`, { error: true });
  const selected = list.slice(0, Math.max(room, 0));
  // Reserve the capacity before decoding: a second paste/drop may arrive while the first
  // batch is still being resized. Sending waits for that same reserved batch to finish.
  let remaining = selected.length;
  composer.pendingImages += remaining;
  composer.refresh();
  try {
    for (const original of selected) {
      const file = await shrinkImage(original, { maxBytes: features.max_image_bytes });
      if (ui !== view || composer.disposed) return;
      composer.pendingImages -= 1; remaining -= 1;
      composer.files.push(file);
    }
  } catch {
    if (ui === view) toast("图片暂时无法添加，请重试。", { error: true });
  } finally {
    composer.pendingImages = Math.max(0, composer.pendingImages - remaining);
    if (ui === view && !composer.disposed) { renderChips(); composer.refresh(); }
  }
}

function renderChips() {
  const { chips, files } = ui.composer;
  chips.hidden = files.length === 0;
  chips.replaceChildren(
    ...files.map((file) =>
      h(
        "span",
        { class: "chip" },
        h("img", { src: previewUrl(file), alt: "待发送的图片" }),
        h("button", { type: "button", class: "chip-remove", "aria-label": "移除图片", onclick: () => {
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
  if (!ui) return;
  const composer = ui.composer;
  if (composer.disposed || composer.pendingImages || composer.voiceStarting || composer.voice) return;
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
  const view = ui;
  if (!view || store.findPending(entry.clientId) !== entry) return;
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
    if (ui !== view) return;
    releasePreviews(entry.files);
    const fresh = store.settle(entry.clientId, result.turn);
    renderLog();
    if (fresh.length) scrollToBottom(true);
  } catch (error) {
    if (ui !== view) return;
    if (!store.findPending(entry.clientId)) return; // the stream already showed it as delivered
    if (error?.name === "AbortError") return;
    if ([401, 403].includes(error.status)) return signedOut();
    entry.state = error.status === 422 ? "rejected" : "failed";
    entry.error = error.status === 0 ? "网络未连接，消息尚未发送。" : error.message;
    renderLog();
  }
}

function editPending(p) {
  if (!ui || store.findPending(p.clientId) !== p) return;
  const composer = ui.composer;
  if (composer.disposed || composer.input.value.trim() || composer.files.length || composer.pendingImages || composer.pendingAudio || composer.voiceStarting || composer.voice) {
    toast("请先发送或清空当前草稿，再编辑这条消息。");
    return;
  }
  if (p.files.length > session.features.max_images) {
    toast("这条消息的图片超过当前限制，请重新选择图片。", { error: true });
    return;
  }
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
  const view = ui;
  if (!view) return;
  const composer = view.composer;
  if (composer.disposed || composer.voice || composer.voiceStarting || composer.pendingImages) return;
  const startedFromMic = document.activeElement === composer.mic;
  composer.voiceStarting = true;
  composer.refresh();
  const limit = session.features.voice_seconds;
  const clock = h("span", { class: "rec-clock", text: "0:00" });
  let cancelled = false;
  let take;
  try {
    take = await record({ maxSeconds: limit, onTick: (seconds) => { clock.textContent = `${durationLabel(seconds)} / ${durationLabel(limit)}`; } });
  } catch (error) {
    if (ui !== view || composer.disposed) return;
    const denied = error?.name === "NotAllowedError";
    toast(denied ? "请在浏览器设置中允许此页面使用麦克风。" : "麦克风暂时无法启动，请重试。", { error: true });
    return;
  } finally {
    composer.voiceStarting = false;
    if (ui === view && !composer.disposed) composer.refresh();
  }
  if (ui !== view || composer.disposed) {
    take.cancel();
    // Permission may finish after sign-out; consume a late recorder failure after release.
    void take.done.catch(() => {});
    return;
  }
  composer.voice = take;
  // Disabling the microphone while permission is pending can move focus to body.
  // Do not steal it back if the person moved on to another control in the meantime.
  const restoreRecordingFocus = document.activeElement === composer.input || (startedFromMic && (!document.activeElement || document.activeElement === document.body || document.activeElement === composer.mic));
  composer.row.hidden = true;
  composer.recording.hidden = false;
  const cancelRecording = iconButton("close", "放弃录音", () => { cancelled = true; take.cancel(); }, "discard");
  const sendRecording = iconButton("send", "发送录音", () => take.stop(), "send primary");
  composer.recording.replaceChildren(
    cancelRecording,
    h("span", { class: "rec-dot", "aria-hidden": "true" }),
    clock,
    sendRecording,
  );
  if (restoreRecordingFocus) cancelRecording.focus({ preventScroll: true });
  try {
    const result = await take.done;
    if (cancelled || !result || ui !== view || composer.disposed) return;
    const entry = store.addPending({ clientId: newClientId(), text: "", voice: result, at: nowSeconds() });
    renderLog();
    scrollToBottom(false);
    await deliver(entry);
  } catch {
    if (ui === view && !composer.disposed) toast("录音未完成，请重试。", { error: true });
  } finally {
    const restore = document.activeElement === cancelRecording || document.activeElement === sendRecording;
    composer.voice = null;
    composer.recording.hidden = true;
    composer.row.hidden = false;
    if (ui === view && !composer.disposed) {
      composer.refresh();
      if (restore) (composer.mic?.hidden === false ? composer.mic : composer.input).focus({ preventScroll: true });
    }
  }
}

// ---- settings -------------------------------------------------------------------------------
async function openSettings() {
  const view = ui;
  if (!view) return;
  const status = h("div", { class: "settings-summary", text: "正在检查连接…" });
  const activity = h("ol", { class: "activity-list", "aria-live": "polite" });
  const connection = h("p", { class: "about", text: connectionLabel(session.connection) });
  const refreshStatus = async () => {
    if (ui !== view || !dialog.open) return;
    try {
      const data = await api("/api/status");
      if (ui !== view || !dialog.open) return;
      const money = value => new Intl.NumberFormat("zh-CN", { style: "currency", currency: "USD", maximumFractionDigits: 3 }).format(value);
      status.replaceChildren(h("p", { class: "about", text: data.working ? "助理正在处理你的请求。" : "已连接，随时可以继续。" }));
      if (Number.isFinite(data.spent_today)) status.append(h("p", { class: "about", text: `今日已用 ${money(data.spent_today)}${Number.isFinite(data.daily_limit) ? ` / 每日额度 ${money(data.daily_limit)}` : ""}` }));
      if (data.pending_approvals > 0) status.append(h("p", { class: "about", text: `${data.pending_approvals} 项操作等待你确认` }));
      connection.textContent = [connectionLabel(data.connection), data.connection?.address].filter(Boolean).join(" · ");
      const items = data.activity?.items ?? [];
      activity.replaceChildren(...(items.length ? items.map((item) => h("li", {},
        h("strong", { text: item.title }),
        h("p", { class: "about", text: activityLabel(item) }),
      )) : [h("li", { class: "about", text: "还没有最近的任务。" })]));
    } catch (error) {
      if (ui === view && dialog.open && error?.name !== "AbortError") status.textContent = error.message;
    }
  };
  const choice = h("div", { class: "segments", role: "group", "aria-label": "外观" });
  let selectedTheme = theme();
  const themeButtons = new Map();
  const paint = () => {
    for (const [value, button] of themeButtons) {
      button.className = selectedTheme === value ? "on" : "";
      button.setAttribute("aria-pressed", String(selectedTheme === value));
    }
    // The thumb under the selected word glides to it (see .segments in style.css).
    choice.style.setProperty?.("--i", String(THEMES.indexOf(selectedTheme)));
    choice.style.setProperty?.("--n", String(THEMES.length));
  };
  for (const value of THEMES) {
    const button = h("button", { type: "button", text: { auto: "跟随系统", light: "浅色", dark: "深色" }[value], onclick: () => {
      selectedTheme = value;
      try { localStorage.setItem("pa-theme", value); } catch { /* The page still uses the selected theme. */ }
      applyTheme(value); paint();
    } });
    themeButtons.set(value, button); choice.append(button);
  }
  paint();
  const hints = [];
  if (!canRecord() && session.features.voice) hints.push("当前浏览器可以发送语音文件。使用加密连接后，也可以直接录音。");
  const ios = /iPhone|iPad/.test(navigator.userAgent);
  const installed = matchMedia("(display-mode: standalone)").matches || navigator.standalone;
  if (ios && !installed) hints.push("在分享菜单中选择「添加到主屏幕」，可像 App 一样打开。首次从主屏幕进入时，需要重新配对。");
  const grabber = h("div", { class: "sheet-grabber", "aria-hidden": "true" });
  const head = h("header", {}, h("h2", { text: "偏好与设置" }), iconButton("close", "关闭", () => dismissSheet(dialog), "close"));
  // Each topic is a titled group: one rounded surface, a hairline between its rows.
  const section = (title, ...rows) => h("section", {}, h("h3", { text: title }), h("div", { class: "group" }, ...rows));
  const dialog = h(
    "dialog",
    { class: "sheet", "aria-label": "偏好与设置" },
    grabber,
    head,
    h(
      "div",
      { class: "sheet-body" },
      section("外观", choice),
      section("最近的任务", activity, h("button", { type: "button", text: "刷新进展", onclick: refreshStatus })),
      section("当前状态", status, connection, h("button", { type: "button", text: "查询完整状态", onclick: () => { dialog.close(); if (ui?.composer.input.value.trim()) toast("请先发送当前消息，再输入 /status 查看完整状态。"); else setAssistantPrompt("/status"); } })),
      section(
        "当前设备",
        h("p", { class: "about", text: session.label || "此浏览器" }),
        h("a", { href: scopedURL("/workspace?tab=devices"), class: "settings-link", text: "管理我的设备与账号" }),
        h("button", { type: "button", class: "danger", text: "退出当前设备", onclick: async () => {
          dialog.close();
          signedOut();
          try {
            await api("/api/logout", { method: "POST" });
          } catch (error) {
            if (error.status !== 401) showPairing("本页内容已清空，但退出尚未确认。请恢复网络后再次退出。");
          }
        } }),
      ),
      hints.length ? section("使用提示", hints.map((hint) => h("p", { class: "about", text: hint }))) : null,
    ),
  );
  dialog.addEventListener("close", () => dialog.remove());
  presentSheet(dialog, { document, handles: [grabber, head] });
  await refreshStatus();
}

// ---- the phone's keyboard, the badge, coming back from the background ---------------------------
function watchViewport() {
  const viewport = window.visualViewport;
  if (!viewport) return;
  const view = ui;
  const fit = () => {
    if (ui !== view) return;
    // Pinch zoom changes the visual viewport too; preserve the person's reading position.
    const keyboard = (viewport.scale ?? 1) === 1 && window.innerHeight - viewport.height > 120;
    root.style.height = keyboard ? `${viewport.height}px` : "";
    root.style.transform = keyboard ? `translateY(${viewport.offsetTop}px)` : "";
    if (keyboard && ui && pinned) scrollToBottom(false);
  };
  viewport.addEventListener("resize", fit);
  viewport.addEventListener("scroll", fit);
  fit();
  return () => {
    viewport.removeEventListener("resize", fit);
    viewport.removeEventListener("scroll", fit);
    root.style.height = "";
    root.style.transform = "";
  };
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
  void ui?.cards?.refresh();
});
window.addEventListener("online", () => { live?.reconnect(); void ui?.cards?.refresh(); });
window.addEventListener("pageshow", (event) => {
  if (event.persisted) { live?.reconnect(); void ui?.cards?.refresh(); }
});

main();
